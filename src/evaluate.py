import os
import time
import numpy as np
import tensorflow as tf

from model import Model, ModelUsage
from data_reader import load_data, DataReader

import argparse

flags = tf.flags

# data
flags.DEFINE_string('data_dir',    'data',   'data directory. Should contain train.txt/valid.txt/test.txt with input data')
flags.DEFINE_string('train_dir',   'cv',     'training directory (models and summaries are saved there periodically)')
flags.DEFINE_string('load_model', "./training/2017-04-17 20-56-12/epoch004_4.9581.model", '(optional) filename of the model to load. Useful for re-starting training from a checkpoint')

# model params
flags.DEFINE_float  ('learning_rate',       1.0,  'starting learning rate')
flags.DEFINE_float  ('max_grad_norm',       5.0,  'normalize gradients at')
flags.DEFINE_integer('rnn_size',        650,                            'size of LSTM internal state')
flags.DEFINE_integer('highway_layers',  2,                              'number of highway layers')
flags.DEFINE_integer('char_embed_size', 15,                             'dimensionality of character embeddings')
flags.DEFINE_string ('kernels',         '[1,2,3,4,5,6,7]',              'CNN kernel widths')
flags.DEFINE_string ('kernel_features', '[50,100,150,200,200,200,200]', 'number of features in the CNN kernel')
flags.DEFINE_integer('rnn_layers',      2,                              'number of layers in the LSTM')
flags.DEFINE_float  ('dropout',         0.5,                            'dropout. 0 = no dropout')

# optimization
flags.DEFINE_integer('num_unroll_steps',    35,   'number of timesteps to unroll for')
flags.DEFINE_integer('batch_size',          20,   'number of sequences to train on in parallel')
flags.DEFINE_integer('max_word_length',     65,   'maximum word length')

# bookkeeping
flags.DEFINE_integer('seed',           3435, 'random number generator seed')
flags.DEFINE_string ('EOS',            '+',  '<EOS> symbol. should be a single unused character (like +) for PTB and blank for others')

FLAGS = flags.FLAGS

def main(_):
    ''' Loads trained model and evaluates it on test split '''

    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--model", help="Model to load")
    args = parser.parse_args()
    
    ''' Loads trained model and evaluates it on test split '''

    if args.model is None:
        print('Please specify checkpoint file to load model from')
        return -1
    
    if not os.path.exists(args.model + '.meta'):
        print('Checkpoint file not found', args.model)
        return -1

    model_path = args.model

    word_vocab, char_vocab, word_tensors, char_tensors, max_word_length = \
        load_data(FLAGS.data_dir, FLAGS.max_word_length, FLAGS.EOS)

    test_reader = DataReader(word_tensors['test'], char_tensors['test'],
                              FLAGS.batch_size, FLAGS.num_unroll_steps, char_vocab)

    print('initialized test dataset reader')

    with tf.Graph().as_default(), tf.Session() as session:

        # tensorflow seed must be inside graph
        tf.set_random_seed(FLAGS.seed)
        np.random.seed(seed=FLAGS.seed)

        ''' build inference graph '''
        with tf.variable_scope("Model"):
            m = Model(FLAGS, char_vocab, word_vocab, max_word_length, ModelUsage.TEST)

            # we need global step only because we want to read it from the model
            global_step = tf.Variable(0, dtype=tf.int32, name='global_step')

        saver = tf.train.Saver()
        saver.restore(session, model_path)
        print('Loaded model from', tf.train.latest_checkpoint(model_path), 'saved at global step', global_step.eval())

        ''' test starts here '''
        rnn_state = session.run(m.initial_rnn_state)
        count = 0
        avg_loss = 0
        start_time = time.time()
        for x, y in test_reader.iter():
            count += 1
            loss, rnn_state = session.run([
                m.loss,
                m.final_rnn_state
            ], {
                m.input  : x,
                m.targets: y,
                m.initial_rnn_state: rnn_state
            })

            avg_loss += loss

        avg_loss /= count
        time_elapsed = time.time() - start_time

        print("test loss = %6.8f, perplexity = %6.8f" % (avg_loss, np.exp(avg_loss)))
        print("test samples:", count*FLAGS.batch_size, "time elapsed:", time_elapsed, "time per one batch:", time_elapsed/count)

if __name__ == "__main__":
    tf.app.run()