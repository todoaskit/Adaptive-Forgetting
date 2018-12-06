from collections import defaultdict

import tensorflow as tf
import numpy as np

from pprint import pprint

from DEN import DEN


class AFN(DEN):

    def __init__(self, den_config):
        super().__init__(den_config)
        self.afn_params = {}
        self.batch_idx = 0
        self.mnist, self.trainXs, self.valXs, self.testXs = None, None, None, None
        self.importance_matrix_tuple = None
        self.old_params_list = []
        self.prediction_history = defaultdict(list)

    def add_dataset(self, mnist, trainXs, valXs, testXs):
        self.mnist, self.trainXs, self.valXs, self.testXs = mnist, trainXs, valXs, testXs

    def afn_create_variable(self, scope, name, shape=None, trainable=True, initializer=None):
        with tf.variable_scope(scope):
            w = tf.get_variable(name, shape, initializer=initializer, trainable=trainable)
            if 'new' not in w.name:
                self.afn_params[w.name] = w
        return w

    def afn_get_variable(self, scope, name, trainable=True):
        with tf.variable_scope(scope, reuse=True):
            w = tf.get_variable(name, trainable=trainable)
            self.afn_params[w.name] = w
        return w

    def afn_create_or_get_variable(self, scope, name, shape=None, trainable=True, initializer=None):
        try:
            w = self.afn_create_variable(scope, name, shape, trainable, initializer)
        except ValueError:
            w = self.afn_get_variable(scope, name, trainable)
        return w

    def clear(self):
        self.destroy_graph()
        self.sess.close()

    def train_den(self, flags):
        params = dict()
        avg_perf = []

        for t in range(flags.n_tasks):
            data = (self.trainXs[t], self.mnist.train.labels,
                    self.valXs[t], self.mnist.validation.labels,
                    self.testXs[t], self.mnist.test.labels)

            self.sess = tf.Session()

            print("\n\n\tTASK %d TRAINING\n" % (t + 1))
            self.task_inc()
            self.load_params(params, time=1)
            perf, sparsity, expansion = self.add_task(t + 1, data)

            print('\n OVERALL EVALUATION')
            params = self.get_params()
            self.clear()
            self.sess = tf.Session()
            self.load_params(params)
            temp_perfs = []
            for j in range(t + 1):
                temp_perf = self.predict_perform(j + 1, self.testXs[j], self.mnist.test.labels)
                temp_perfs.append(temp_perf)
            avg_perf.append(sum(temp_perfs) / float(t + 1))
            print("   [*] avg_perf: %.4f" % avg_perf[t])

            if t != flags.n_tasks - 1:
                self.clear()

    def predict_only_after_training(self):
        print("\n PREDICT ONLY AFTER TRAINING")
        self.sess = tf.Session()
        temp_perfs = []
        for t in range(self.T):
            temp_perf = self.predict_perform(t + 1, self.testXs[t], self.mnist.test.labels)
            temp_perfs.append(temp_perf)
        return temp_perfs

    def initialize_batch(self):
        self.batch_idx = 0

    def get_next_batch(self, x, y, batch_size=None):
        batch_size = batch_size if batch_size else self.batch_size
        next_idx = self.batch_idx + batch_size
        r = x[self.batch_idx:next_idx], y[self.batch_idx:next_idx]
        self.batch_idx = next_idx
        return r

    def recover_recent_params(self):
        print("\n RECOVER RECENT PARAMS")
        self.recover_params(-1)

    def recover_old_params(self):
        print("\n RECOVER RECENT PARAMS")
        self.recover_params(0)

    def recover_params(self, idx):
        self.params = self.old_params_list[idx]
        self.clear()
        self.sess = tf.Session()
        self.load_params(self.params)
        self.sess.run(tf.global_variables_initializer())

    def print_history(self, one_step_neuron=1):
        for policy, history in self.prediction_history.items():
            print(policy)
            for i, acc in enumerate(history):
                print("\t".join([str((i+1)*one_step_neuron)] + [str(x) for x in acc]))

    def adaptive_forget(self, task_to_forget, number_of_neurons, policy):
        assert policy in ["EIN", "LIN", "RANDOM"]

        print("\n ADAPTIVE FORGET {} task-{} from {}, neurons-{}".format(
            policy, task_to_forget, self.T, number_of_neurons))

        self.old_params_list.append(self.get_params())

        ni_1, ni_2 = [], []
        if policy == "EIN":
            ni_1, ni_2 = self.get_exceptionally_important_neurons_for_t(task_to_forget, number_of_neurons)
        elif policy == "LIN":
            ni_1, ni_2 = self.get_least_important_neurons_for_others(task_to_forget, number_of_neurons)

        self._remove_neurons("layer1", ni_1)
        self._remove_neurons("layer2", ni_2)

        params = self.get_params()
        self.clear()
        self.sess = tf.Session()
        self.load_params(params)

    def sequentially_adaptive_forget_and_predict(self, task_to_forget, one_step_neurons, steps, policy):

        print("\n SEQUENTIALLY ADAPTIVE FORGET {} task-{} from {}, neurons-{}".format(
            policy, task_to_forget, self.T, one_step_neurons * steps))

        for i in range(steps+1):
            self.adaptive_forget(task_to_forget, i * one_step_neurons, policy)
            pred = self.predict_only_after_training()
            self.prediction_history[policy].append(pred)
            self.recover_recent_params()

    def _remove_neurons(self, scope, indexes):
        print("\n REMOVE NEURONS {} - {}".format(scope, indexes))

        w: tf.Variable = self.get_variable(scope, "weight", False)
        b: tf.Variable = self.get_variable(scope, "biases", False)

        val_w = w.eval(session=self.sess)
        val_b = b.eval(session=self.sess)

        for i in indexes:
            val_w[:, i] = 0
            val_b[i] = 0

        self.sess.run(tf.assign(w, val_w))
        self.sess.run(tf.assign(b, val_b))

        self.params[w.name] = w
        self.params[b.name] = b

    # shape = (|h|,) or tuple of (|h1|,), (|h2|,)
    def get_importance_vector(self, task_id, layer_separate=False):
        print("\n GET IMPORTANCE VECTOR OF TASK %d" % task_id)

        X = tf.placeholder(tf.float32, [None, self.dims[0]])
        Y = tf.placeholder(tf.float32, [None, self.n_classes])

        hidden_layer_list = []
        bottom = X
        stamp = self.time_stamp['task%d' % task_id]
        for i in range(1, self.n_layers):
            w = self.get_variable('layer%d' % i, 'weight', True)
            b = self.get_variable('layer%d' % i, 'biases', True)
            w = w[:stamp[i - 1], :stamp[i]]
            b = b[:stamp[i]]

            afn_w = self.afn_create_or_get_variable("afn_t%d_layer%d" % (task_id, i), "weight",
                                                    trainable=True, initializer=w)
            afn_b = self.afn_create_or_get_variable("afn_t%d_layer%d" % (task_id, i), "biases",
                                                    trainable=True, initializer=b)
            bottom = tf.nn.relu(tf.matmul(bottom, afn_w) + afn_b)
            hidden_layer_list.append(bottom)
            print(' [*] task %d, shape of layer %d : %s' % (task_id, i, afn_w.get_shape().as_list()))

        w = self.get_variable('layer%d' % self.n_layers, 'weight_%d' % task_id, True)
        b = self.get_variable('layer%d' % self.n_layers, 'biases_%d' % task_id, True)
        w = w[:stamp[self.n_layers - 1], :stamp[self.n_layers]]
        b = b[:stamp[self.n_layers]]
        afn_w = self.afn_create_or_get_variable("afn_t%d_layer%d" % (task_id, self.n_layers), "weight_%d" % task_id,
                                                trainable=True, initializer=w)
        afn_b = self.afn_create_or_get_variable("afn_t%d_layer%d" % (task_id, self.n_layers), "biases_%d" % task_id,
                                                trainable=True, initializer=b)

        y = tf.matmul(bottom, afn_w) + afn_b
        yhat = tf.nn.sigmoid(y)
        loss = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(logits=y, labels=Y))

        train_step = tf.train.GradientDescentOptimizer(self.init_lr).minimize(loss)
        gradient_list = [tf.gradients(loss, h) for h in hidden_layer_list]

        self.sess.run(tf.global_variables_initializer())

        h_length_list = [h.get_shape().as_list()[-1] for h in hidden_layer_list]
        importance_vector_1 = np.zeros(shape=(0, h_length_list[0]))
        importance_vector_2 = np.zeros(shape=(0, h_length_list[1]))

        self.initialize_batch()
        while True:
            batch_x, batch_y = self.get_next_batch(self.trainXs[task_id - 1], self.mnist.train.labels)
            if len(batch_x) == 0:
                break

            hidden_1, hidden_2, gradient_1, gradient_2 = self.sess.run(
                hidden_layer_list + gradient_list,
                feed_dict={X: batch_x, Y: batch_y}
            )

            # shape = batch_size * |h|
            batch_importance_vector_1 = np.absolute(hidden_1 * gradient_1)[0]
            importance_vector_1 = np.vstack((importance_vector_1, batch_importance_vector_1))

            batch_importance_vector_2 = np.absolute(hidden_2 * gradient_2)[0]
            importance_vector_2 = np.vstack((importance_vector_2, batch_importance_vector_2))

        importance_vector_1 = importance_vector_1.sum(axis=0)
        importance_vector_2 = importance_vector_2.sum(axis=0)

        if layer_separate:
            return importance_vector_1, importance_vector_2  # (|h1|,), (|h2|,)
        else:
            return np.concatenate((importance_vector_1, importance_vector_2))  # shape = (|h|,)

    # shape = (T, |h|) or (T, |h1|), (T, |h2|)
    def get_importance_matrix(self, layer_separate=False):

        importance_matrix_1, importance_matrix_2 = None, None

        for t in reversed(range(1, self.T + 1)):
            iv_1, iv_2 = self.get_importance_vector(task_id=t, layer_separate=True)

            if t == self.T:
                importance_matrix_1 = np.zeros(shape=(0, iv_1.shape[0]))
                importance_matrix_2 = np.zeros(shape=(0, iv_2.shape[0]))

            importance_matrix_1 = np.vstack((
                np.pad(iv_1, (0, importance_matrix_1.shape[-1] - iv_1.shape[0]), 'constant', constant_values=(0, 0)),
                importance_matrix_1,
            ))
            importance_matrix_2 = np.vstack((
                np.pad(iv_2, (0, importance_matrix_2.shape[-1] - iv_2.shape[0]), 'constant', constant_values=(0, 0)),
                importance_matrix_2,
            ))

        self.importance_matrix_tuple = importance_matrix_1, importance_matrix_2
        if layer_separate:
            return self.importance_matrix_tuple  # (T, |h1|), (T, |h2|)
        else:
            return np.concatenate(self.importance_matrix_tuple, axis=1)  # shape = (T, |h|)

    # Inappropriate for T=2
    def get_exceptionally_important_neurons_for_t(self, task_id, number_to_select):

        if not self.importance_matrix_tuple:
            self.get_importance_matrix()

        i_mat = np.concatenate(self.importance_matrix_tuple, axis=1)
        num_neurons = i_mat.shape[-1]

        mean_dot_j = np.mean(i_mat, axis=0)
        stdev_dot_j = np.std(i_mat, axis=0)

        ei = np.zeros(shape=(num_neurons,))
        for j in range(num_neurons):
            if stdev_dot_j[j] != 0:
                ei[j] = (i_mat[task_id - 1][j] - mean_dot_j[j])/stdev_dot_j[j]
            else:
                ei[j] = - np.inf

        ei_desc_sorted_idx = np.argsort(ei)[::-1]
        selected = ei_desc_sorted_idx[:number_to_select]

        divider = self.importance_matrix_tuple[0].shape[-1]
        return selected[selected < divider], (selected[selected >= divider] - divider)

    def get_least_important_neurons_for_others(self, task_id, number_to_select):

        if not self.importance_matrix_tuple:
            self.get_importance_matrix()

        i_mat = np.concatenate(self.importance_matrix_tuple, axis=1)
        i_mat = np.delete(i_mat, task_id - 1, axis=0)

        mean_dot_j = np.mean(i_mat, axis=0)

        mean_asc_sorted_idx = np.argsort(mean_dot_j)
        selected = mean_asc_sorted_idx[:number_to_select]

        divider = self.importance_matrix_tuple[0].shape[-1]
        return selected[selected < divider], (selected[selected >= divider] - divider)
