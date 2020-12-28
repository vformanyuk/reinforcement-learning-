import gym
import numpy as np
import tensorflow as tf
import multiprocessing as mp

from APEX.neural_networks import policy_network, critic_network
from APEX.APEX_Local_MemoryBuffer import APEX_NStepReturn_RandomAccess_MemoryBuffer

class Actor(object):
    def __init__(self, id:int, batch_size:int, gamma:float, actor_leraning_rate:float, critic_learning_rate:float,
                 cmd_pipe:mp.Pipe, weights_pipe:mp.Pipe, replay_pipe:mp.Pipe, cancelation_token:mp.Value, training_active_flag:mp.Value,
                 *args, **kwargs):
        self.debug_mode = False
        self.id = id
        self.cmd_pipe = cmd_pipe
        self.weights_pipe = weights_pipe
        self.replay_pipe = replay_pipe
        self.cancelation_token = cancelation_token
        self.training_active = training_active_flag
        self.exchange_steps = 100 + np.random.randint(low=10, high=90, size=1)[0]
        self.data_send_steps = 50
        self.actor_optimizer = tf.keras.optimizers.Adam(actor_leraning_rate)
        self.critic_optimizer = tf.keras.optimizers.Adam(critic_learning_rate)
        self.gamma = gamma
        self.batch_size = batch_size
        self.N = 3

    def log(self, msg):
        if self.debug_mode:
            print(f'[Actor {self.id}] {msg}')

    def get_target_weights(self):
        try:
            self.cmd_pipe.send([0, self.id])
            weights = self.weights_pipe.recv()
            self.target_actor.set_weights(weights[0])
            self.target_critic.set_weights(weights[1])
            self.log(f'Target actor and target critic weights refreshed.')
        except EOFError:
            print("[get_target_weights] Connection closed.")
        except OSError:
            print("[get_target_weights] Connection closed.")

    def send_replay_data(self, states, actions, next_states, rewards, gamma_powers, dones, td_errors):
        buffer = []
        for i in range(len(states)):
            buffer.append([states[i], actions[i], next_states[i], rewards[i], gamma_powers[i], dones[i], td_errors[i]])
        try:
            self.cmd_pipe.send([1, self.id])
            self.log(f'Replay data command sent.')
            self.replay_pipe.send(buffer)
            self.log(f'Replay data sent.')
        except EOFError:
            print("[send_replay_data] Connection closed.")
        except OSError:
            print("[send_replay_data] Connection closed.")

    @tf.function
    def train_actor_critic(self, states, actions, next_states, rewards, gammas, dones):
        target_mu = self.target_actor(next_states, training=False)
        target_q = rewards + tf.math.pow(self.gamma, gammas + 1) * tf.reduce_sum((1 - dones) * self.target_critic([next_states, target_mu], training=False), axis = 1)

        with tf.GradientTape() as tape:
            current_q = tf.squeeze(self.critic([states, actions], training=True), axis=1)
            #c_loss = self.mse_loss(current_q, target_q)
            td_errors = target_q - current_q
            c_loss = tf.reduce_mean(tf.math.pow(td_errors, 2))
        gradients = tape.gradient(c_loss, self.critic.trainable_variables)
        self.critic_optimizer.apply_gradients(zip(gradients, self.critic.trainable_variables))

        with tf.GradientTape() as tape:
            current_mu = self.actor(states, training=True)
            current_q = self.critic([states, current_mu], training=False)
            a_loss = tf.reduce_mean(-current_q)
        gradients = tape.gradient(a_loss, self.actor.trainable_variables)
        self.actor_optimizer.apply_gradients(zip(gradients, self.actor.trainable_variables))
        return c_loss, a_loss, td_errors

    def run(self):
        # this configuration must be done for every module
        gpus = tf.config.experimental.list_physical_devices('GPU')
        tf.config.experimental.set_memory_growth(gpus[0], True)

        env = gym.make('LunarLanderContinuous-v2')
        self.actor = policy_network((env.observation_space.shape[0]), env.action_space.shape[0])
        self.target_actor = policy_network((env.observation_space.shape[0]), env.action_space.shape[0])
        
        self.critic = critic_network((env.observation_space.shape[0]), env.action_space.shape[0])
        self.target_critic = critic_network((env.observation_space.shape[0]), env.action_space.shape[0])

        self.get_target_weights()

        exp_buffer = APEX_NStepReturn_RandomAccess_MemoryBuffer(512, self.N, self.gamma, env.observation_space.shape, env.action_space.shape)
        rewards_history = []
        global_step = 0
        for i in range(1000):
            if self.cancelation_token.value != 0:
                break
            
            done = False
            observation = env.reset()

            episodic_reward = 0
            epoch_steps = 0
            critic_loss_history = []
            actor_loss_history = []

            while not done and self.cancelation_token.value == 0:
                chosen_action = self.actor(np.expand_dims(observation, axis = 0), training=False)[0].numpy()# + action_noise()
                next_observation, reward, done, _ = env.step(chosen_action)
                exp_buffer.store(observation, chosen_action, next_observation, reward, float(done))

                if global_step > 2 * self.batch_size:
                    replay_states, replay_actions, replay_next_states, replay_rewards, replay_gammas, replay_dones, idxs = exp_buffer(self.batch_size)
                    actor_loss, critic_loss, td_errors = self.train_actor_critic(replay_states, replay_actions, replay_next_states, replay_rewards, replay_gammas, replay_dones) 
                    exp_buffer.update_td_errors(idxs, td_errors)
                    actor_loss_history.append(actor_loss)
                    critic_loss_history.append(critic_loss)

                if global_step % (self.data_send_steps + self.N) == 0 and global_step > 0:
                    self.send_replay_data(*exp_buffer.get_transfer_data(self.data_send_steps))

                if global_step % self.exchange_steps == 0 and self.training_active.value > 0: # update target networks every 'exchange_steps'
                    self.get_target_weights()

                observation = next_observation
                global_step+=1
                epoch_steps+=1
                episodic_reward += reward

            rewards_history.append(episodic_reward)
            last_mean = np.mean(rewards_history[-100:])
            print(f'[{self.id} {i} ({epoch_steps})] Actor_Loss: {np.mean(actor_loss_history):.4f} Critic_Loss: {np.mean(critic_loss_history):.4f} Total reward: {episodic_reward} Mean(100)={last_mean:.4f}')
            if last_mean > 200:
                self.actor.save(f'lunar_lander_apex_dpg_{self.id}.h5')
                break
        env.close()


def RunActor(id:int, batch_size:int, gamma:float, actor_leraning_rate:float, critic_learning_rate:float,
             cmd_pipe:mp.Pipe, weights_pipe:mp.Pipe, replay_data_pipe:mp.Pipe, cancelation_token:mp.Value, training_active_flag:mp.Value):
    actor = Actor(id, batch_size, gamma, actor_leraning_rate, critic_learning_rate, cmd_pipe, weights_pipe, replay_data_pipe, cancelation_token, training_active_flag)
    actor.run()