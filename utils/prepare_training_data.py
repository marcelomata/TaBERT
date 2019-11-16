import multiprocessing
import os, sys
import time
import traceback
from argparse import ArgumentParser, Namespace
from multiprocessing import connection
from typing import List, Iterator, Callable
import numpy as np
import json
import ujson
import msgpack
import signal

import gc
import torch
import zmq

from pathlib import Path
from tqdm import tqdm, trange

from random import shuffle, choice, sample, random

from pytorch_pretrained_bert import *

from table_bert.input_formatter import VanillaTableBertInputFormatter, TableBertBertInputFormatter
from table_bert.config import TableBertConfig
from table_bert.dataset import Example, TableDatabase

TRAIN_INSTANCE_QUEUE_ADDRESS = 'tcp://127.0.0.1:15566'
EXAMPLE_QUEUE_ADDRESS = 'tcp://127.0.0.1:15567'
DATABASE_SERVER_ADDR = 'localhost'


def sample_context(example: Example, max_context_length: int, context_sample_strategy: str = 'nearest') -> Iterator:
    if context_sample_strategy == 'nearest':
        selected_context = []

        context_before, context_after = example.context[0], example.context[1]
        context_src = 'before'
        if not context_before:
            context = context_after
            context_src = 'after'
        elif not context_after:
            context = context_before
        elif random() < 0.5:
            context = context_after
            context_src = 'after'
        else:
            context = context_before

        if context_src == 'before':
            for i in reversed(range(0, len(context))):
                sent = context[i]
                selected_context = sent + selected_context

                if len(selected_context) > max_context_length:
                    selected_context = selected_context[-max_context_length:]  # only keep context close to the table
                    break
        elif context_src == 'after':
            for i in range(0, len(context)):
                sent = context[i]
                selected_context = selected_context + sent

                if len(selected_context) > max_context_length:
                    selected_context = selected_context[:max_context_length]  # only keep context close to the table
                    break

        if selected_context:
            yield selected_context
    elif context_sample_strategy == 'concate_and_enumerate':
        # concatenate the context before and after, select a random chunk of text
        all_context = example.context[0] + example.context[1]
        selected_context = []
        for i in range(len(all_context)):
            sent = all_context[i]
            selected_context.extend(sent)
            if len(selected_context) > max_context_length:
                selected_context = selected_context[:max_context_length]

                if selected_context:
                    yield selected_context
                selected_context = []

        if selected_context:
            yield selected_context
    else:
        raise RuntimeError('Unknown context sample strategy')


def generate_train_instance_from_example(
    table_db: TableDatabase,
    indices: List[int],
    status_queue: multiprocessing.Queue,
    bert_input_formatter: TableBertBertInputFormatter,
    debug_file: Path = None
):
    context = zmq.Context()
    instance_sender = context.socket(zmq.PUSH)
    # instance_sender.setsockopt(zmq.LINGER, -1)
    instance_sender.connect(TRAIN_INSTANCE_QUEUE_ADDRESS)

    table_db.restore_redis_client()

    if debug_file:
        f_dbg = open(debug_file, 'w')

    num_processed = 0
    for idx in indices:
        example = table_db[idx]
        try:
            instances = bert_input_formatter.get_pretraining_instances_from_example(example, sample_context)

            for instance in instances:
                if debug_file and random() <= 0.05:
                    f_dbg.write(json.dumps(instance) + os.linesep)

                bert_input_formatter.remove_unecessary_instance_entries(instance)
                # del instance['tokens']
                # del instance['masked_lm_labels']
                # del instance['info']

                # instance_sender.send_pyobj(ujson.dumps(instance, ensure_ascii=False))
                data = msgpack.packb(instance, use_bin_type=True)
                instance_sender.send_pyobj(data)

            num_processed += 1
            if num_processed == 5000:
                status_queue.put(('HEART_BEAT', num_processed))
                num_processed = 0
        except:
            # raise
            typ, value, tb = sys.exc_info()
            print('*' * 50 + 'Exception' + '*' * 50, file=sys.stderr)
            print(example.serialize(), file=sys.stderr)
            print('*' * 50 + 'Stack Trace' + '*' * 50, file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            # print('*' * 50 + 'Exception' + '*' * 50, file=sys.stderr)

            sys.stderr.flush()

    instance_sender.send_pyobj(None)
    status_queue.put('EXIT')

    if debug_file:
        f_dbg.close()

    while True:
        time.sleep(10)


def write_instance_to_file(
        output_file: Path,
        num_workers: int,
        stat_send: connection.Connection,
        input_formatter: VanillaTableBertInputFormatter,
        shard_size: int = 3000000
):
    context = zmq.Context()
    instance_receiver = context.socket(zmq.PULL)
    # instance_receiver.setsockopt(zmq.LINGER, -1)
    instance_receiver.bind(TRAIN_INSTANCE_QUEUE_ADDRESS)

    finished_worker_num = 0
    num_instances = 0
    shard_id = 0

    sequences = []
    segment_a_lengths = []
    sequence_offsets = []
    masked_lm_positions = []
    masked_lm_label_ids = []
    masked_lm_offsets = []

    def _save_shard():
        nonlocal shard_id

        data = {
            'sequences': np.uint16(sequences),
            'segment_a_lengths': np.uint16(segment_a_lengths),
            'sequence_offsets': np.int64(sequence_offsets),
            'masked_lm_positions': np.uint16(masked_lm_positions),
            'masked_lm_label_ids': np.uint16(masked_lm_label_ids),
            'masked_lm_offsets': np.int64(masked_lm_offsets)
        }

        tgt_file = output_file.with_name(output_file.name + f'.shard{shard_id}.bin')
        torch.save(data, str(tgt_file), pickle_protocol=4)

        shard_id += 1
        del sequences[:]
        del segment_a_lengths[:]
        del sequence_offsets[:]
        del masked_lm_positions[:]
        del masked_lm_label_ids[:]
        del masked_lm_offsets[:]

    while True:
        data = instance_receiver.recv_pyobj()
        if data is not None:
            data = msgpack.unpackb(data, raw=False)
            # data = ujson.loads(data)
             # print('received one')

            cur_pos = len(sequences)
            sequence_len = len(data['token_ids'])
            sequences.extend(data['token_ids'])
            segment_a_lengths.append(data['segment_a_length'])
            sequence_offsets.append([cur_pos, cur_pos + sequence_len])

            cur_pos = len(masked_lm_positions)
            lm_mask_len = len(data['masked_lm_positions'])
            masked_lm_positions.extend(data['masked_lm_positions'])
            masked_lm_label_ids.extend(data['masked_lm_label_ids'])
            masked_lm_offsets.append([cur_pos, cur_pos + lm_mask_len])

            num_instances += 1

            if num_instances > 0 and num_instances % shard_size == 0:
                _save_shard()
        else:
            finished_worker_num += 1
            if finished_worker_num == num_workers:
                break

    if len(sequences) > 0:
        _save_shard()

    stat_send.send((num_instances, shard_id))
    instance_receiver.close()
    context.destroy()


def generate_for_epoch(table_db: TableDatabase,
                       indices: List[int],
                       epoch_file: Path,
                       metrics_file: Path,
                       input_formatter: TableBertBertInputFormatter,
                       instance_serialization_func: Callable,
                       args: Namespace):
    # print(f'Generating {epoch_file}', file=sys.stderr)

    stat_recv, stat_send = multiprocessing.Pipe()
    num_workers = multiprocessing.cpu_count() - 2

    instance_writer_process = multiprocessing.Process(target=instance_serialization_func,
                                                      args=(epoch_file, num_workers, stat_send, input_formatter),
                                                      daemon=True)

    debug = True

    workers = []
    worker_status_queue = multiprocessing.Queue()
    # table_bert_config = TableBertConfig.from_dict(vars(args))
    # input_formatter = VanillaTableBertInputFormatter(table_bert_config)
    for i in range(num_workers):
        indices_chunk = indices[i: len(indices): num_workers]
        worker_process = multiprocessing.Process(
            target=generate_train_instance_from_example,
            args=(table_db, indices_chunk, worker_status_queue, input_formatter,
                  epoch_file.with_suffix('.sample.json') if debug and i == 0 and 'epoch_0' in str(epoch_file) else None),
            daemon=True
        )
        worker_process.start()
        workers.append(worker_process)

    while not all(worker.is_alive() for worker in workers):
        time.sleep(1)

    instance_writer_process.start()

    finished_worker_num = 0
    with tqdm(desc=f"Generating dataset {epoch_file}", file=sys.stdout) as pbar:
        while True:
            status = worker_status_queue.get()
            if status == 'EXIT':
                finished_worker_num += 1
                if finished_worker_num == num_workers:
                    break
            elif status[0] == 'HEART_BEAT':
                num_processed = status[1]
                pbar.update(num_processed)

    num_instances, shard_num = stat_recv.recv()
    print('num instances:', num_instances)
    instance_writer_process.join()

    for worker in workers:
        worker.terminate()

    with metrics_file.open('w') as f:
        metrics = {
            "num_training_examples": num_instances,
            "max_seq_len": args.max_sequence_len,
            "shard_num": shard_num
        }
        f.write(json.dumps(metrics))


def graceful_exit(sig=None, frame=None):
    os.system('redis-cli FLUSHALL')
    exit(0)


def init_redis():
    # All the configuration parameters set using CONFIG SET
    # are immediately loaded by Redis and will take effect
    # starting with the next command executed.

    os.system('redis-cli CONFIG SET save ""')
    os.system('redis-cli CONFIG SET appendonly no')
    os.system('redis-cli CONFIG SET maxmemory 450gb')
    os.system('redis-cli CONFIG SET stop-writes-on-bgsave-error no')

    signal.signal(signal.SIGINT, graceful_exit)


def main():
    parser = ArgumentParser()
    parser.add_argument('--train_corpus', type=Path, required=True)
    parser.add_argument("--output_dir", type=Path, required=True)
    parser.add_argument("--epochs_to_generate", type=int, default=3,
                        help="Number of epochs of preprocess to pregenerate")
    parser.add_argument('--no_wiki_tables_from_common_crawl', action='store_true', default=False)

    TableBertConfig.add_args(parser)

    args = parser.parse_args()

    init_redis()

    table_bert_config = TableBertConfig.from_dict(vars(args))
    tokenizer = BertTokenizer.from_pretrained(args.base_model_name, do_lower_case=args.do_lower_case)
    input_formatter = VanillaTableBertInputFormatter(table_bert_config, tokenizer)

    with TableDatabase.from_jsonl(args.train_corpus, tokenizer=tokenizer) as table_db:
        args.output_dir.mkdir(exist_ok=True, parents=True)
        print(f'Num tables in total: {len(table_db)}', file=sys.stdout)

        # generate train and dev split
        example_indices = list(range(len(table_db)))
        shuffle(example_indices)
        dev_size = min(int(len(table_db) * 0.1), 100000)
        train_indices = example_indices[:-dev_size]
        dev_indices = example_indices[-dev_size:]

        with (args.output_dir / 'config.json').open('w') as f:
            json.dump(vars(args), f, indent=2, sort_keys=True, default=str)

        (args.output_dir / 'train').mkdir(exist_ok=True)
        (args.output_dir / 'dev').mkdir(exist_ok=True)

        # generate dev preprocess first
        dev_file = args.output_dir / 'dev' / 'epoch_0'
        dev_metrics_file = args.output_dir / 'dev' / "epoch_0.metrics.json"
        generate_for_epoch(table_db, dev_indices, dev_file, dev_metrics_file, input_formatter, write_instance_to_file, args)

        for epoch in trange(args.epochs_to_generate, desc='Epoch'):
            gc.collect()
            epoch_filename = args.output_dir / 'train' / f"epoch_{epoch}"
            metrics_file = args.output_dir / 'train' / f"epoch_{epoch}.metrics.json"
            generate_for_epoch(table_db, train_indices, epoch_filename, metrics_file, input_formatter, write_instance_to_file, args)

    graceful_exit()


if __name__ == '__main__':
    main()
