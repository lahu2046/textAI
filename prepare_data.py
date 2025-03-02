"""
Turn a merged corpus into tfrecord files.

NOTE: You will want to do this using several processes. I did this on an AWS machine with 72 CPUs using GNU parallel
as that's where I had the deduplicated RealNews dataset.
"""
import argparse
import ujson as json
# from sample.encoder import get_encoder, tokenize_for_grover_training, detokenize, sliding_window, create_int_feature
import random
import tensorflow as tf
import collections
import os
from tempfile import TemporaryDirectory

from tokenization import tokenization

parser = argparse.ArgumentParser(description='SCRAPE!')
parser.add_argument(
    '-fold',
    dest='fold',
    default=0,
    type=int,
    help='which fold we are on'
)
parser.add_argument(
    '-num_folds',
    dest='num_folds',
    default=1,
    type=int,
    help='Number of folds (corresponding to both the number of training files and the number of testing files)',
)
parser.add_argument(
    '-seed',
    dest='seed',
    default=1337,
    type=int,
    help='which seed to use'
)
parser.add_argument(
    '-base_fn',
    dest='base_fn',
    default='news2016zh_',
    type=str,
    help='We will output files that are like {base_fn}_{n}.tfrecord for n in 0, ..., 1023'
)

parser.add_argument(
    '-input_fn',
    dest='input_fn',
    default='realnews.jsonl',
    type=str,
    help='Base filename to use. THIS MUST BE A LOCAL FILE.'
)
parser.add_argument(
    '-max_seq_length',
    dest='max_seq_length',
    default=1025,
    type=int,
    help='Max sequence length',
)


args = parser.parse_args()
random.seed(args.seed + args.fold)

#encoder = get_encoder()
tokenizer = tokenization.FullTokenizer(
    vocab_file="tokenization/bert-base-chinese-vocab.txt", do_lower_case=True)


class S3TFRecordWriter(object):
    def __init__(self, fn):
        self.fn = fn
        if fn.startswith('s3://'):
            from boto3.s3.transfer import TransferConfig
            import boto3
            self.gclient = None
            self.s3client = boto3.client('s3',
                                         )
            self.storage_dir = TemporaryDirectory()
            self.writer = tf.python_io.TFRecordWriter(
                os.path.join(self.storage_dir.name, 'temp.tfrecord'))
            self.bucket_name, self.file_name = self.fn.split(
                's3://', 1)[1].split('/', 1)
        elif fn.startswith('gs://'):
            from google.cloud import storage
            self.s3client = None
            self.gclient = storage.Client()
            self.storage_dir = TemporaryDirectory()
            self.writer = tf.python_io.TFRecordWriter(
                os.path.join(self.storage_dir.name, 'temp.tfrecord'))
            self.bucket_name, self.file_name = self.fn.split(
                'gs://', 1)[1].split('/', 1)

        else:
            self.s3client = None
            self.gclient = None
            self.bucket_name = None
            self.file_name = None
            self.storage_dir = None
            self.writer = tf.python_io.TFRecordWriter(fn)

    def write(self, x):
        self.writer.write(x)

    def close(self):
        self.writer.close()

        if self.s3client is not None:
            from boto3.s3.transfer import TransferConfig
            config = TransferConfig(multipart_threshold=1024 * 25, max_concurrency=10,
                                    multipart_chunksize=1024 * 25, use_threads=True)
            self.s3client.upload_file(
                os.path.join(self.storage_dir.name, 'temp.tfrecord'),
                self.bucket_name,
                self.file_name,
                ExtraArgs={'ACL': 'public-read'}, Config=config,
            )
            self.storage_dir.cleanup()
        if self.gclient is not None:
            bucket = self.gclient.get_bucket(self.bucket_name)
            blob = bucket.blob(self.file_name)
            blob.upload_from_filename(os.path.join(
                self.storage_dir.name, 'temp.tfrecord'))
            self.storage_dir.cleanup()

    def __enter__(self):
        # Called when entering "with" context.
        return self

    def __exit__(self, *_):
        # Called when exiting "with" context.
        # Upload shit
        print("CALLING CLOSE")
        self.close()


def article_iterator(tokenizer):
    """ Iterate through the provided filename + tokenize"""
    assert os.path.exists(args.input_fn)
    for (dirpath, dirnames, filenames) in os.walk(args.input_fn):
        for filename in filenames:
            with open(os.path.join(dirpath, filename), 'r', encoding="utf-8") as f:
                lines = f.readlines()
            lines = [line for line in lines]
            for l_no, l in enumerate(lines):
                article = {}
                article['text'] = l
                line = tokenization.convert_to_unicode(article['text'])  # for news2016zh text body
                tokens = tokenizer.tokenize(line)
                input_ids = tokenizer.convert_tokens_to_ids(tokens)
                article['input_ids'] = input_ids
                article['inst_index'] = (l_no // args.num_folds)
                if article['inst_index'] < 100:
                    print('---\nINPUT{}. {}\n---\nTokens: {}\n'.format(article['inst_index'], tokens, input_ids),
                          flush=True)
                if len(article['input_ids']) <= 64:  # min size of article
                    continue
                yield article


def create_int_feature(values):
    feature = tf.train.Feature(
        int64_list=tf.train.Int64List(value=list(values)))
    return feature


def buffered_and_sliding_window_article_iterator(tokenizer, final_desired_size=1025):
    """ We apply a sliding window to fix long sequences, and use a buffer that combines short sequences."""
    for article in article_iterator(tokenizer):
        if len(article['input_ids']) >= final_desired_size:
            article['input_ids'] = article['input_ids'][0:final_desired_size-1]
        while len(article['input_ids']) < final_desired_size:
            article['input_ids'].append(0)
        yield article


# OK now write the tfrecord file
total_written = 0
train_file = args.base_fn + 'train_wiki19_{:04d}.tfrecord'.format(args.fold)
with S3TFRecordWriter(train_file) as train_writer:
    for article in buffered_and_sliding_window_article_iterator(tokenizer,
                                                                final_desired_size=max(args.max_seq_length + 1, 1025)):
        writer2use = train_writer
        assert len(article['input_ids']) == (args.max_seq_length + 1)

        features = collections.OrderedDict()
        features["input_ids"] = create_int_feature(article['input_ids'])
        tf_example = tf.train.Example(
            features=tf.train.Features(feature=features))

        writer2use.write(tf_example.SerializeToString())
        total_written += 1

        # DEBUG
        if article['inst_index'] < 5:
            print("~~~\nIndex {}. ARTICLE: {}\n---\nTokens: {}\n\n".format(article['inst_index'],
                                                                           tokenizer.convert_ids_to_tokens(
                                                                               article['input_ids']),
                                                                           article['input_ids']
                                                                           ), flush=True)
        if article['inst_index'] % 1000 == 0:
            print("{} articles, {} written".format(
                article['inst_index'], total_written), flush=True)
print("DONE UPLOADING", flush=True)
