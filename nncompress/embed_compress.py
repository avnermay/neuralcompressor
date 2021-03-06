from __future__ import absolute_import, division, print_function

import sys
import os
import time
import math
import logging # Avner change
import tensorflow as tf
import numpy as np
# Import _linear
if tuple(map(int, tf.__version__.split("."))) >= (1, 6, 0):
    from tensorflow.contrib.rnn.python.ops import core_rnn_cell
    _linear = core_rnn_cell._linear
else:
    from tensorflow.python.ops.rnn_cell_impl import _linear

tf.flags.DEFINE_string('qmats', "data/glove.6B.300d.quant.npy", "output")

class EmbeddingCompressor(object):

    def __init__(self, n_codebooks, n_centroids, model_path,
            learning_rate=0.0001, batch_size=64, grad_clip=0.001, tau=1.0): # Avner change
        """
        M: number of codebooks (subcodes)
        K: number of vectors in each codebook
        model_path: prefix for saving or loading the parameters
        """
        self.M = n_codebooks
        self.K = n_centroids
        self._model_path = model_path
        # Avner begin change
        self._LEARNING_RATE = learning_rate
        self._BATCH_SIZE = batch_size
        self._GRAD_CLIP = grad_clip
        self._TAU = tau
        # Avner end change

    def _gumbel_dist(self, shape, eps=1e-20):
        U = tf.random_uniform(shape,minval=0,maxval=1)
        return -tf.log(-tf.log(U + eps) + eps)

    def _sample_gumbel_vectors(self, logits, temperature):
        y = logits + self._gumbel_dist(tf.shape(logits))
        return tf.nn.softmax( y / temperature)

    def _gumbel_softmax(self, logits, temperature, sampling=True):
        """Compute gumbel softmax.

        Without sampling the gradient will not be computed
        """
        if sampling:
            y = self._sample_gumbel_vectors(logits, temperature)
        else:
            k = tf.shape(logits)[-1]
            y_hard = tf.cast(tf.equal(y,tf.reduce_max(y,1,keep_dims=True)),y.dtype)
            y = tf.stop_gradient(y_hard - y) + y
        return y

    def _encode(self, input_matrix, word_ids, embed_size):
        input_embeds = tf.nn.embedding_lookup(input_matrix, word_ids, name="input_embeds")

        M, K = self.M, self.K

        with tf.variable_scope("h"):
            h = tf.nn.tanh(_linear(input_embeds, M * K/2, True))
        with tf.variable_scope("logits"):
            logits = _linear(h, M * K, True)
            logits = tf.log(tf.nn.softplus(logits) + 1e-8)
        logits = tf.reshape(logits, [-1, M, K], name="logits")
        return input_embeds, logits

    def _decode(self, gumbel_output, codebooks):
        return tf.matmul(gumbel_output, codebooks)

    def _reconstruct(self, codes, codebooks):
        return None

    def build_export_graph(self, embed_matrix):
        """Export the graph for exporting codes and codebooks.

        Args:
            embed_matrix: numpy matrix of original embeddings
        """
        vocab_size = embed_matrix.shape[0]
        embed_size = embed_matrix.shape[1]

        input_matrix = tf.constant(embed_matrix, name="embed_matrix")
        word_ids = tf.placeholder_with_default(
            np.array([3,4,5], dtype="int32"), shape=[None], name="word_ids")

        # Define codebooks
        codebooks = tf.get_variable("codebook", [self.M * self.K, embed_size])

        # Coding
        input_embeds, logits = self._encode(input_matrix, word_ids, embed_size)  # ~ (B, M, K)
        codes = tf.cast(tf.argmax(logits, axis=2), tf.int32)  # ~ (B, M)

        # Reconstruct
        offset = tf.range(self.M, dtype="int32") * self.K
        codes_with_offset = codes + offset[None, :]

        selected_vectors = tf.gather(codebooks, codes_with_offset)  # ~ (B, M, H)
        reconstructed_embed = tf.reduce_sum(selected_vectors, axis=1)  # ~ (B, H)
        return word_ids, codes, reconstructed_embed

    def build_training_graph(self, embed_matrix):
        """Export the training graph.

        Args:
            embed_matrix: numpy matrix of original embeddings
        """
        vocab_size = embed_matrix.shape[0]
        embed_size = embed_matrix.shape[1]

        # Define input variables
        input_matrix = tf.constant(embed_matrix, name="embed_matrix")
        tau = tf.placeholder_with_default(np.array(1.0, dtype='float32'), tuple()) - 0.1
        word_ids = tf.placeholder_with_default(
            np.array([3,4,5], dtype="int32"), shape=[None], name="word_ids")

        # Define codebooks
        codebooks = tf.get_variable("codebook", [self.M * self.K, embed_size])

        # Encoding
        input_embeds, logits = self._encode(input_matrix, word_ids, embed_size)  # ~ (B, M, K)

        # Discretization
        D = self._gumbel_softmax(logits, self._TAU, sampling=True)
        gumbel_output = tf.reshape(D, [-1, self.M * self.K])  # ~ (B, M * K)
        maxp = tf.reduce_mean(tf.reduce_max(D, axis=2))

        # Decoding
        y_hat = self._decode(gumbel_output, codebooks)

        # Define loss
        loss = 0.5 * tf.reduce_sum((y_hat - input_embeds)**2, axis=1)
        loss = tf.reduce_mean(loss, name="loss")

        # Define optimization
        max_grad_norm = self._GRAD_CLIP # Avner change
        tvars = tf.trainable_variables()
        grads = tf.gradients(loss, tvars)
        grads, global_norm = tf.clip_by_global_norm(grads, max_grad_norm)
        global_norm = tf.identity(global_norm, name="global_norm")
        optimizer = tf.train.AdamOptimizer(self._LEARNING_RATE)
        train_op = optimizer.apply_gradients(zip(grads, tvars), name="train_op")

        return word_ids, loss, train_op, maxp

    def train(self, embed_matrix, max_epochs=200):
        """Train the model for compress `embed_matrix` and save to `model_path`.

        Args:
            embed_matrix: a numpy matrix
        """
        dca_train_log = [] # Avner change
        vocab_size = embed_matrix.shape[0]
        valid_ids = np.random.RandomState(3).randint(0, vocab_size, size=(self._BATCH_SIZE * 10,)).tolist()
        # Training
        with tf.Graph().as_default(), tf.Session() as sess:
            with tf.variable_scope("Graph", initializer=tf.random_uniform_initializer(-0.01, 0.01)):
                word_ids_var, loss_op, train_op, maxp_op = self.build_training_graph(embed_matrix)
            # Initialize variables
            tf.global_variables_initializer().run()
            best_loss = 100000
            saver = tf.train.Saver()

            vocab_list = list(range(vocab_size))
            for epoch in range(max_epochs):
                start_time = time.time()
                train_loss_list = []
                train_maxp_list = []
                np.random.shuffle(vocab_list)
                for start_idx in range(0, vocab_size, self._BATCH_SIZE):
                    word_ids = vocab_list[start_idx:start_idx + self._BATCH_SIZE]
                    loss, _, maxp = sess.run(
                        [loss_op, train_op, maxp_op],
                        {word_ids_var: word_ids}
                    )
                    train_loss_list.append(loss)
                    train_maxp_list.append(maxp)

                # Print every epoch
                time_elapsed = time.time() - start_time
                batches_per_second = len(train_loss_list) / time_elapsed # Avner change

                # Validation
                valid_loss_list = []
                valid_maxp_list = []
                for start_idx in range(0, len(valid_ids), self._BATCH_SIZE):
                    word_ids = valid_ids[start_idx:start_idx + self._BATCH_SIZE]
                    loss, maxp = sess.run(
                        [loss_op, maxp_op],
                        {word_ids_var: word_ids}
                    )
                    valid_loss_list.append(loss)
                    valid_maxp_list.append(maxp)

                # Avner begin change
                train_loss = np.mean(train_loss_list)
                train_maxp = np.mean(train_maxp_list)
                valid_loss = np.mean(valid_loss_list)
                valid_maxp = np.mean(valid_maxp_list)
                # Avner end change
                report_token = ""
                if valid_loss <= best_loss * 0.999:
                    report_token = "*"
                    best_loss = valid_loss
                    saver.save(sess, self._model_path)
                # Avner begin change
                log_str = "[epoch{}] trian_loss={:.2f} train_maxp={:.2f} valid_loss={:.2f} valid_maxp={:.2f} batches_per_second={:.0f} time_elapsed={:.2f} {}".format(
                    epoch, train_loss, train_maxp, valid_loss, valid_maxp,
                    batches_per_second, time_elapsed, report_token)
                logging.info(log_str)
                dca_train_log.append(
                    {"epoch": epoch,
                     "train_loss" : float(train_loss),
                     "train_maxp" : float(train_maxp),
                     "valid_loss" : float(valid_loss),
                     "valid_maxp" : float(valid_maxp),
                     "batches_per_second" : batches_per_second,
                     "time_elapsed" : time_elapsed,
                     "report_token" : report_token}
                )
                # Avner end change
        logging.info("Training Done") # Avner change
        return dca_train_log # Avner change

    def export(self, embed_matrix, prefix):
        """Export word codes and codebook for given embedding.

        Args:
            embed_matrix: original embedding
            prefix: prefix of saving path
        """
        assert os.path.exists(self._model_path + ".meta")
        vocab_size = embed_matrix.shape[0]
        with tf.Graph().as_default(), tf.Session() as sess:
            with tf.variable_scope("Graph"):
                word_ids_var, codes_op, reconstruct_op = self.build_export_graph(embed_matrix)
            saver = tf.train.Saver()
            saver.restore(sess, self._model_path)

            # Dump codebook
            codebook_tensor = sess.run(sess.graph.get_tensor_by_name('Graph/codebook:0')) # Avner change
            np.save(prefix + ".codebook", codebook_tensor)

            # Dump codes
            codes_to_return = [] # Avner change
            with open(prefix + ".codes", "w") as fout:
                vocab_list = list(range(embed_matrix.shape[0]))
                for start_idx in range(0, vocab_size, self._BATCH_SIZE):
                    word_ids = vocab_list[start_idx:start_idx + self._BATCH_SIZE]
                    codes = sess.run(codes_op, {word_ids_var: word_ids}).tolist()
                    for code in codes:
                        codes_to_return.append(code) # Avner change
                        fout.write(" ".join(map(str, code)) + "\n")
        return codes_to_return, codebook_tensor  # Avner change

    def evaluate(self, embed_matrix):
        assert os.path.exists(self._model_path + ".meta")
        vocab_size = embed_matrix.shape[0]
        with tf.Graph().as_default(), tf.Session() as sess:
            with tf.variable_scope("Graph"):
                word_ids_var, codes_op, reconstruct_op = self.build_export_graph(embed_matrix)
            saver = tf.train.Saver()
            saver.restore(sess, self._model_path)

            vocab_list = list(range(embed_matrix.shape[0]))
            distances = []
            for start_idx in range(0, vocab_size, self._BATCH_SIZE):
                word_ids = vocab_list[start_idx:start_idx + self._BATCH_SIZE]
                reconstructed_vecs = sess.run(reconstruct_op, {word_ids_var: word_ids})
                original_vecs = embed_matrix[start_idx:start_idx + self._BATCH_SIZE]
                distances.extend(np.linalg.norm(reconstructed_vecs - original_vecs, axis=1).tolist())
            frob_squared_error = np.sum([d**2 for d in distances]) # Avner change
        return np.mean(distances), frob_squared_error # Avner change
