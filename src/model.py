from __future__ import print_function
from __future__ import division


import tensorflow as tf
from tensorflow.contrib.tensorboard.plugins import projector
from enum import Enum

class ModelUsage(Enum):
    TRAIN = 0
    VALIDATE = 1
    TEST = 2
    USE = 3

class adict(dict):
    ''' Attribute dictionary - a convenience data structure, similar to SimpleNamespace in python 3.3
        One can use attributes to read/write dictionary content.
    '''
    def __init__(self, *av, **kav):
        dict.__init__(self, *av, **kav)
        self.__dict__ = self

class Model:
    def __init__(self, flags, char_vocab, word_vocab, max_word_length, model_usage=ModelUsage.TRAIN, metadata = ""):
        self.flags = flags
        self.char_vocab = char_vocab
        self.word_vocab = word_vocab
        self.max_word_length = max_word_length
        self.model_usage = model_usage
        if metadata != "":
            self.projector_config = projector.ProjectorConfig()
        else:
            self.projector_config = 0
        self.metadata = metadata

        builders = {
            ModelUsage.TRAIN : self.build_train_graph,
            ModelUsage.VALIDATE : self.build_loss_graph,
            ModelUsage.TEST : self.build_loss_graph,
            ModelUsage.USE : self.build_inference_graph
        }
        builders.get(self.model_usage)()
        
    def build_train_graph(self):
        self.build_loss_graph()
        self.training_graph(
                        loss=self.loss * self.flags.num_unroll_steps, 
                        learning_rate=self.flags.learning_rate, 
                        max_grad_norm=self.flags.max_grad_norm)

    def build_loss_graph(self):
        self.build_inference_graph()
        self.loss_graph(
                        logits=self.logits, 
                        batch_size=self.flags.batch_size, 
                        num_unroll_steps=self.flags.num_unroll_steps)

    def build_inference_graph(self):
        self.inference_graph(
                    char_vocab_size=self.char_vocab.size,
                    word_vocab_size=self.word_vocab.size,
                    char_embed_size=self.flags.char_embed_size,
                    batch_size=self.flags.batch_size,
                    num_highway_layers=self.flags.highway_layers,
                    num_lstm_layers=self.flags.rnn_layers,
                    rnn_size=self.flags.rnn_size,
                    max_word_length=self.max_word_length,
                    kernels=eval(self.flags.kernels),
                    kernel_features=eval(self.flags.kernel_features),
                    num_unroll_steps=self.flags.num_unroll_steps,
                    dropout=self.flags.dropout)

    def linear(self, input_, output_size, scope=None):
        '''
        Linear map: output[k] = sum_i(Matrix[k, i] * args[i] ) + Bias[k]

        Args:
            args: a tensor or a list of 2D, batch x n, Tensors.
        output_size: int, second dimension of W[i].
        scope: VariableScope for the created subgraph; defaults to "Linear".
        Returns:
            A 2D Tensor with shape [batch x output_size] equal to
            sum_i(args[i] * W[i]), where W[i]s are newly created matrices.
        Raises:
            ValueError: if some of the arguments has unspecified or wrong shape.
        '''

        shape = input_.get_shape().as_list()
        if len(shape) != 2:
            raise ValueError("Linear is expecting 2D arguments: %s" % str(shape))
        if not shape[1]:
            raise ValueError("Linear expects shape[1] of arguments: %s" % str(shape))
        input_size = shape[1]

        # Now the computation.
        with tf.variable_scope(scope or "SimpleLinear"):
            matrix = tf.get_variable("Matrix", [output_size, input_size], dtype=input_.dtype)
            bias_term = tf.get_variable("Bias", [output_size], dtype=input_.dtype)

        return tf.matmul(input_, tf.transpose(matrix)) + bias_term

    def highway(self, input_, size, num_layers=1, bias=-2.0, f=tf.nn.relu, scope='Highway'):
        """Highway Network (cf. http://arxiv.org/abs/1505.00387).
        t = sigmoid(Wy + b)
        z = t * g(Wy + b) + (1 - t) * y
        where g is nonlinearity, t is transform gate, and (1 - t) is carry gate.
        """

        with tf.variable_scope(scope):
            for idx in range(num_layers):
                g = f(self.linear(input_, size, scope='highway_lin_%d' % idx))

                t = tf.sigmoid(self.linear(input_, size, scope='highway_gate_%d' % idx) + bias)

                output = t * g + (1. - t) * input_
                input_ = output

        return output

    def conv2d(self, input_, output_dim, k_h, k_w, name="conv2d"):
        with tf.variable_scope(name):
            w = tf.get_variable('w', [k_h, k_w, input_.get_shape()[-1], output_dim])
            b = tf.get_variable('b', [output_dim])

        return tf.nn.conv2d(input_, w, strides=[1, 1, 1, 1], padding='VALID') + b

    def conv2dLayers(self, input_, kernels, kernel_features, scope='TDNN'):
        '''

        :input:           input float tensor of shape [(batch_size*num_unroll_steps) x max_word_length x embed_size]
        :kernels:         array of kernel sizes
        :kernel_features: array of kernel feature sizes (parallel to kernels)
        '''
        assert len(kernels) == len(kernel_features), 'Kernel and Features must have the same size'

        max_word_length = input_.get_shape()[1]
        embed_size = input_.get_shape()[-1]

        # input_: [batch_size*num_unroll_steps, 1, max_word_length, embed_size]
        input_ = tf.expand_dims(input_, 1)

        layers = []
        with tf.variable_scope(scope):
            for kernel_size, kernel_feature_size in zip(kernels, kernel_features):
                reduced_length = max_word_length - kernel_size + 1

                # [batch_size x max_word_length x embed_size x kernel_feature_size]
                conv = self.conv2d(input_, kernel_feature_size, 1, kernel_size, name="kernel_%d" % kernel_size)

                # [batch_size x 1 x 1 x kernel_feature_size]
                pool = tf.nn.max_pool(tf.tanh(conv), [1, 1, reduced_length, 1], [1, 1, 1, 1], 'VALID')

                layers.append(tf.squeeze(pool, [1, 2]))

            if len(kernels) > 1:
                output = tf.concat(layers, 1)
            else:
                output = layers[0]

        return output

    def inference_graph(self,
                        char_vocab_size, 
                        word_vocab_size,
                        char_embed_size=15,
                        batch_size=20,
                        num_highway_layers = 2,
                        num_lstm_layers = 2,
                        rnn_size=650,
                        max_word_length=65,
                        kernels         = [ 1,   2,   3,   4,   5,   6,   7],
                        kernel_features = [50, 100, 150, 200, 200, 200, 200],
                        num_unroll_steps=35,
                        dropout=0.0):

        assert len(kernels) == len(kernel_features), 'Kernel and Features must have the same size'

        self.input = tf.placeholder(tf.int32, shape=[batch_size, num_unroll_steps, max_word_length], name="input")

        ''' First, embed characters '''
        with tf.variable_scope('Embedding'):
            char_embedding = tf.get_variable('char_embedding', [char_vocab_size, char_embed_size])
            if self.projector_config:
                embedding = self.projector_config.embeddings.add()
                embedding.tensor_name = char_embedding.name
                embedding.metadata_path = self.metadata
            ''' this op clears embedding vector of first symbol (symbol at position 0, which is by convention the position
            of the padding symbol). It can be used to mimic Torch7 embedding operator that keeps padding mapped to
            zero embedding vector and ignores gradient updates. For that do the following in TF:
            1. after parameter initialization, apply this op to zero out padding embedding vector
            2. after each gradient update, apply this op to keep padding at zero'''
            self.clear_char_embedding_padding = tf.scatter_update(char_embedding, [0], tf.constant(0.0, shape=[1, char_embed_size]))

            # [batch_size x max_word_length, num_unroll_steps, char_embed_size]
            input_embedded = tf.nn.embedding_lookup(char_embedding, self.input)

            input_embedded = tf.reshape(input_embedded, [-1, max_word_length, char_embed_size])

        ''' Second, apply convolutions '''
       
        # [batch_size x num_unroll_steps, cnn_size]  where cnn_size=sum(kernel_features)
        output_cnn = self.conv2dLayers(input_embedded, kernels, kernel_features)

        if num_highway_layers > 0:
            output_cnn = self.highway(output_cnn, output_cnn.get_shape()[-1], num_layers=num_highway_layers)

        ''' Finally, do LSTM '''
        with tf.variable_scope('LSTM'):
            def create_rnn_cell():
                cell = tf.contrib.rnn.BasicLSTMCell(rnn_size, state_is_tuple=True, forget_bias=0.0)
                if dropout:
                    cell = tf.contrib.rnn.DropoutWrapper(cell, output_keep_prob=1.-dropout)
                return cell

            cell = tf.contrib.rnn.MultiRNNCell([create_rnn_cell() for _ in range(num_lstm_layers)], state_is_tuple=True)

            self.initial_rnn_state = cell.zero_state(batch_size, dtype=tf.float32)

            output_cnn = tf.reshape(output_cnn, [batch_size, num_unroll_steps, -1])
            output_cnn2 = [tf.squeeze(x, [1]) for x in tf.split(output_cnn, num_unroll_steps, 1)]

            outputs, self.final_rnn_state = tf.contrib.rnn.static_rnn(cell, output_cnn2, initial_state=self.initial_rnn_state, dtype=tf.float32)

            # linear projection onto output (word) vocab
            self.logits = []
            with tf.variable_scope('WordEmbedding') as scope:
                for idx, output in enumerate(outputs):
                    if idx > 0:
                        scope.reuse_variables()
                    self.logits.append(self.linear(output, word_vocab_size))

    def loss_graph(self, logits, batch_size, num_unroll_steps):

        with tf.variable_scope('Loss'):
            self.targets = tf.placeholder(tf.int64, [batch_size, num_unroll_steps], name='targets')
            target_list = [tf.squeeze(x, [1]) for x in tf.split(self.targets, num_unroll_steps, 1)]

            self.loss = tf.reduce_mean(tf.nn.sparse_softmax_cross_entropy_with_logits(logits=logits, labels=target_list), name='loss')

    def training_graph(self, loss, learning_rate=1.0, max_grad_norm=5.0):
        ''' Builds training graph. '''
        self.global_step = tf.Variable(0, name='global_step', trainable=False)

        with tf.variable_scope('SGD_Training'):
            # SGD learning parameter
            self.learning_rate = tf.Variable(learning_rate, trainable=False, name='learning_rate')

            # collect all trainable variables
            tvars = tf.trainable_variables()
            grads, self.global_norm = tf.clip_by_global_norm(tf.gradients(loss, tvars), max_grad_norm)

            optimizer = tf.train.GradientDescentOptimizer(learning_rate)
            self.train_op = optimizer.apply_gradients(zip(grads, tvars), global_step=self.global_step)

def model_size():
    params = tf.trainable_variables()
    size = 0
    for x in params:
        sz = 1
        for dim in x.get_shape():
            sz *= dim.value
        size += sz
    return size