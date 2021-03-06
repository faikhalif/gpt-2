#!/usr/bin/env python3
# coding: utf-8
# Usage:
#  PYTHONPATH=src ./train --dataset <file|directory|glob>

import argparse
import json
import os
import numpy as np
import tensorflow as tf
import time
import tqdm
from tensorflow.core.protobuf import rewriter_config_pb2

import model, sample, encoder
from load_dataset import load_dataset, Sampler
from accumulate import AccumulatingOptimizer
import memory_saving_gradients

CHECKPOINT_DIR = 'checkpoint'
SAMPLE_DIR = 'samples'


parser = argparse.ArgumentParser(
    description='Fine-tune GPT-2 on your custom dataset.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--dataset', metavar='PATH', type=str, required=True, help='Input file, directory, or glob pattern (utf-8 text, or preencoded .npz files).')
parser.add_argument('--model_name', metavar='MODEL', type=str, default='117M', help='Pretrained model name')
parser.add_argument('--combine', metavar='CHARS', type=int, default=50000, help='Concatenate input files with <|endoftext|> separator into chunks of this minimum size')
parser.add_argument('--encoding', type=str, default='utf-8', help='Set the encoding for reading and writing files.')

parser.set_defaults(bpe=True)
parser.add_argument("--no-bpe", dest="bpe", action="store_false")
parser.add_argument("--vocabulary", type=str, metavar="PATH",
                    help="Specify an explicit vocabulary file for the encoder.")

parser.add_argument('--batch_size', metavar='SIZE', type=int, default=1, help='Batch size')
parser.add_argument('--learning_rate', metavar='LR', type=float, default=0.00002, help='Learning rate for Adam')
parser.add_argument('--accumulate_gradients', metavar='N', type=int, default=1, help='Accumulate gradients across N minibatches.')
parser.add_argument('--memory_saving_gradients', default=False, action='store_true', help='Use gradient checkpointing to reduce vram usage.')
parser.add_argument('--only_train_transformer_layers', default=False, action='store_true', help='Restrict training to the transformer blocks.')
parser.add_argument('--optimizer', type=str, default='adam', help='Optimizer. <adam|sgd>.')
parser.add_argument('--noise', type=float, default=0.0, help='Add noise to input training data to regularize against typos.')

parser.add_argument('--top_k', type=int, default=40, help='K for top-k sampling.')
parser.add_argument('--top_p', type=float, default=0.0, help='P for top-p sampling. Overrides top_k if set > 0.')

parser.add_argument('--restore_from', type=str, default='latest', help='Either "latest", "fresh", or a path to a checkpoint file')
parser.add_argument('--run_name', type=str, default='run1', help='Run id. Name of subdirectory in checkpoint/ and samples/')
parser.add_argument("--checkpoint_dir", type=str, default=CHECKPOINT_DIR)
parser.add_argument('--sample_every', metavar='N', type=int, default=100, help='Generate samples every N steps')
parser.add_argument('--sample_length', metavar='TOKENS', type=int, default=1023, help='Sample this many tokens')
parser.add_argument('--sample_num', metavar='N', type=int, default=1, help='Generate this many samples')
parser.add_argument('--save_every', metavar='N', type=int, default=1000, help='Write a checkpoint every N steps')

parser.add_argument('--val_dataset', metavar='PATH', type=str, default=None, help='Dataset for validation loss, defaults to --dataset.')
parser.add_argument('--val_batch_size', metavar='SIZE', type=int, default=2, help='Batch size for validation.')
parser.add_argument('--val_batch_count', metavar='N', type=int, default=40, help='Number of batches for validation.')
parser.add_argument('--val_every', metavar='STEPS', type=int, default=0, help='Calculate validation loss every STEPS steps.')
parser.add_argument('--eval_dataset', metavar='PATH', type=str, default=None, help='Dataset for evaluation.')
parser.add_argument('--eval', default=False, action='store_true', help='Evaluate to get surprisals.')
parser.add_argument('--fpath', type=str, default=None, help='Path to write surprisals of evaluation data to file')

parser.add_argument("--just_ppl", default=False, action="store_true")


def maketree(path):
    try:
        os.makedirs(path)
    except:
        pass


def randomize(context, hparams, p):
    if p > 0:
        mask = tf.random.uniform(shape=tf.shape(context)) < p
        noise = tf.random.uniform(shape=tf.shape(context), minval=0, maxval=hparams.n_vocab, dtype=tf.int32)
        return tf.where(mask, noise, context)
    else:
        return context


def load_eval_dataset(enc, path, encoding=None):
    with open(path, 'r', encoding=encoding) as f:
        lines = f.readlines()
    enc_lines = [enc.encode(line.strip()) for line in lines]
    return enc_lines


def main():
    args = parser.parse_args()

    if args.bpe:
        enc = encoder.get_encoder(args.model_name)
    else:
        with open(args.vocabulary, "r") as f:
            vocab = json.load(f)
        enc = encoder.DisabledEncoder(vocab)
    print(enc, type(enc))

    hparams = model.default_hparams()
    with open(os.path.join('models', args.model_name, 'hparams.json')) as f:
        hparams.override_from_dict(json.load(f))

    # Use encoder vocabulary size. Will crash if there is an encoder -- model
    # mismatch.
    if hparams.n_vocab != enc.vocab_size:
        print("Updating hparams to use n_vocab = %i from encoder." % enc.vocab_size)
        hparams.n_vocab = enc.vocab_size

    if args.sample_length > hparams.n_ctx:
        raise ValueError(
            "Can't get samples longer than window size: %s" % hparams.n_ctx)

    if args.model_name == '345M':
        args.memory_saving_gradients = True
        if args.optimizer == 'adam':
            args.only_train_transformer_layers = True

    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    config.graph_options.rewrite_options.layout_optimizer = rewriter_config_pb2.RewriterConfig.OFF
    with tf.Session(config=config) as sess:
        context = tf.placeholder(tf.int32, [args.batch_size, None])
        context_in = randomize(context, hparams, args.noise)
        output = model.model(hparams=hparams, X=context_in)
        loss = tf.reduce_mean(
            tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=context[:, 1:], logits=output['logits'][:, :-1]))

        if args.val_every > 0:
            val_context = tf.placeholder(tf.int32, [args.val_batch_size, None])
            val_output = model.model(hparams=hparams, X=val_context)
            val_loss = tf.reduce_mean(
                tf.nn.sparse_softmax_cross_entropy_with_logits(
                    labels=val_context[:, 1:], logits=val_output['logits'][:, :-1]))
            val_loss_summary = tf.summary.scalar('val_loss', val_loss)

        if args.eval:
            # val_context = tf.placeholder(tf.int32, [args.val_batch_size, None])
            # val_output = model.model(hparams=hparams, X=val_context)
            val_logprobs = tf.nn.sparse_softmax_cross_entropy_with_logits(
                    labels=val_context[:, 1:], logits=val_output['logits'][:, :-1])

        all_vars = [v for v in tf.trainable_variables() if 'model' in v.name]
        train_vars = [v for v in all_vars if '/h' in v.name] if args.only_train_transformer_layers else all_vars

        saver = tf.train.Saver(
            var_list=all_vars,
            max_to_keep=5,
            keep_checkpoint_every_n_hours=2)
        sess.run(tf.global_variables_initializer())

        if args.restore_from == 'latest':
            ckpt = tf.train.latest_checkpoint(
                os.path.join(args.checkpoint_dir, args.run_name))
            if ckpt is None:
                # Get fresh GPT weights if new run.
                ckpt = tf.train.latest_checkpoint(
                    os.path.join('models', args.model_name))
        elif args.restore_from == 'fresh':
            ckpt = tf.train.latest_checkpoint(
                os.path.join('models', args.model_name))
        else:
            ckpt = tf.train.latest_checkpoint(args.restore_from)
        print('Loading checkpoint', ckpt)
        saver.restore(sess, ckpt)

        print('batch size:', args.batch_size)

        print('Loading dataset...')
        print(args.dataset)
        print(args.val_dataset)
        # chunks = load_dataset(enc, args.dataset, args.combine, encoding=args.encoding)
        # data_sampler = Sampler(chunks)
        # if args.val_every > 0:
        #     if args.val_dataset:
        #         val_chunks = load_dataset(enc, args.val_dataset, args.combine, encoding=args.encoding)
        #     else:
        #         val_chunks = chunks
        # print('dataset has', data_sampler.total_size, 'tokens')

        if args.eval_dataset:
            print(args.eval_dataset)
            eval_sents = load_eval_dataset(enc, args.eval_dataset, encoding=args.encoding)

        counter = 1
        counter_path = os.path.join(args.checkpoint_dir, args.run_name, 'counter')
        if os.path.exists(counter_path):
            # Load the step number if we're resuming a run
            # Add 1 so we don't immediately try to save again
            with open(counter_path, 'r') as fp:
                counter = int(fp.read()) + 1

        def get_surprisals():
            print('Get surprisals...')
            logprobs_list = []
            for sent in tqdm.tqdm(eval_sents):
                logprobs_list.append(sess.run(val_logprobs, feed_dict={val_context: args.val_batch_size * [sent]})[0])

            with open(args.fpath, 'w', encoding="utf-8") as f:
                # Write header.
                f.write("sentence_id\ttoken_id\ttoken\tsurprisal\n")

                for sent_index, sent in enumerate(eval_sents):
                    words = enc.decode(sent).split()

                    # for token_index, token in enumerate(sent):
                    #     if token_index == 0:
                    #         f.write(str(sent_index+1)+'\t'+str(token_index+1)+'\t'+enc.decode([token])+'\t0.0\n')
                    #     else:
                    #         f.write(str(sent_index+1)+'\t'+str(token_index+1)+'\t'+enc.decode([token])+'\t'+str(-np.log2(np.exp(-logprobs_list[sent_index][token_index-1])))+'\n')

                    surprisals = []
                    token_concat = ''
                    surprisal_sum = 0
                    word_index = 0
                    for token_index, token in enumerate(sent):
                        # print(token_concat)
                        token_concat += enc.decode([token]).strip()

                        if token_index > 0:
                            surprisal_sum += -np.log2(np.exp(-logprobs_list[sent_index][token_index-1]))
                        if token_concat == words[word_index]:
                            f.write(str(sent_index+1)+'\t'+str(word_index+1)+'\t'+words[word_index]+'\t'+str(surprisal_sum)+'\n')
                            token_concat = ''
                            surprisal_sum = 0
                            word_index += 1
                    # print(word_index, len(words))
                    assert word_index == len(words)

        def get_ppl():
            print('Get perplexity...')
            logprobs_list = []
            for sent in tqdm.tqdm(eval_sents):
                logprobs_list.append(sess.run(val_logprobs, feed_dict={val_context: args.val_batch_size * [sent]})[0])
            total_surprisal = 0
            total_wcount = 0
            for sent_index, sent in enumerate(eval_sents):
                words = enc.decode(sent).split()
                total_wcount += len(words)
                for token_index, token in enumerate(sent):
                    if token_index == 0:
                        continue

                    try:
                        total_surprisal += -np.log2(np.exp(-logprobs_list[sent_index][token_index-1]))
                    except:
                        import pdb; pdb.set_trace()
            ppl = np.exp(total_surprisal/total_wcount)
            print('Perplexity:', ppl)


        def sample_batch():
            return [data_sampler.sample(1024) for _ in range(args.batch_size)]


        avg_loss = (0.0, 0.0)
        start_time = time.time()

        try:
            if args.just_ppl:
                get_ppl()
            else:
                get_surprisals()
        except KeyboardInterrupt:
            print('interrupted')
            save()


if __name__ == '__main__':
    main()
