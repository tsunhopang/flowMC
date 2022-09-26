from logging import lastResort
from typing import Callable, Tuple
import jax
import jax.numpy as jnp
import numpy as np
from flowMC.nfmodel.utils import sample_nf, make_training_loop
from flowMC.sampler.NF_proposal import make_nf_metropolis_sampler
from flax.training import train_state  # Useful dataclass to keep train state
import flax
import optax


class Sampler():
    """
    Sampler class that host configuration parameters, NF model, and local sampler

    Args:
        n_dim (int): Dimension of the problem.
        rng_key_set (Tuple): Tuple of random number generator keys.
        local_sampler (Callable): Local sampler maker
        sampler_params (dict): Parameters for the local sampler.
        likelihood (Callable): Likelihood function.
        nf_model (Callable): Normalizing flow model.
        n_loop_training (int, optional): Number of training loops. Defaults to 2.
        n_loop_production (int, optional): Number of production loops. Defaults to 2.
        n_local_steps (int, optional): Number of local steps per loop. Defaults to 5.
        n_global_steps (int, optional): Number of global steps per loop. Defaults to 5.
        n_chains (int, optional): Number of chains. Defaults to 5.
        n_epochs (int, optional): Number of epochs per training loop. Defaults to 5.
        learning_rate (float, optional): Learning rate for the NF model. Defaults to 0.01.
        max_samples (int, optional): Maximum number of samples fed to training the NF model. Defaults to 10000.
        momentum (float, optional): Momentum for the NF model. Defaults to 0.9.
        batch_size (int, optional): Batch size for the NF model. Defaults to 10.
        use_global (bool, optional): Whether to use global sampler. Defaults to True.
        logging (bool, optional): Whether to log the training process. Defaults to True.
        nf_variable (None, optional): Mean and variance variables for the NF model. Defaults to None.
        keep_quantile (float, optional): Quantile of chains to keep when training the normalizing flow model. Defaults to 0.5.
        local_autotune (None, optional): Auto-tune function for the local sampler. Defaults to None.

    Methods:
        sample: Sample from the posterior using the local sampler.
        sampling_loop: Sampling loop for the NF model.
        local_sampler_tuning: Tune the local sampler.
        global_sampler_tuning: Tune the global sampler.
        production_run: Run the production run.
        get_sampler_state: Get the sampler state.
        sample_flow: Sample from the normalizing flow model.


    """

    def __init__(
        self,
        n_dim: int,
        rng_key_set: Tuple,
        local_sampler: Callable,
        sampler_params: dict,
        likelihood: Callable,
        nf_model: Callable,
        n_loop_training: int = 2,
        n_loop_production: int = 2,
        n_local_steps: int = 5,
        n_global_steps: int = 5,
        n_chains: int = 5,
        n_epochs: int = 5,
        learning_rate: float = 0.01,
        max_samples: int = 10000,
        momentum: float = 0.9,
        batch_size: int = 10,
        use_global: bool = True,
        logging: bool = True,
        nf_variable=None,
        keep_quantile=0,
        local_autotune=None,
    ):
        rng_key_init, rng_keys_mcmc, rng_keys_nf, init_rng_keys_nf = rng_key_set

        self.likelihood = likelihood
        self.likelihood_vec = jax.jit(jax.vmap(self.likelihood))
        self.sampler_params = sampler_params
        self.local_sampler = local_sampler(likelihood)
        self.local_autotune = local_autotune

        self.rng_keys_nf = rng_keys_nf
        self.rng_keys_mcmc = rng_keys_mcmc
        self.n_dim = n_dim
        self.n_loop_training = n_loop_training
        self.n_loop_production = n_loop_production
        self.n_local_steps = n_local_steps
        self.n_global_steps = n_global_steps
        self.n_chains = n_chains
        self.n_epochs = n_epochs
        self.learning_rate = learning_rate
        self.max_samples = max_samples
        self.momentum = momentum
        self.batch_size = batch_size
        self.use_global = use_global
        self.logging = logging

        self.nf_model = nf_model
        model_init = nf_model.init(init_rng_keys_nf, jnp.ones((1, self.n_dim)))
        params = model_init["params"]
        self.variables = model_init["variables"]
        if nf_variable is not None:
            self.variables = self.variables

        self.keep_quantile = keep_quantile
        self.nf_training_loop, train_epoch, train_step = make_training_loop(
            self.nf_model
        )
        self.global_sampler = make_nf_metropolis_sampler(self.nf_model)

        tx = optax.adam(self.learning_rate, self.momentum)
        self.state = train_state.TrainState.create(
            apply_fn=nf_model.apply, params=params, tx=tx
        )

        training = {}
        training["chains"] = jnp.empty((self.n_chains, 0, self.n_dim))
        training["log_prob"] = jnp.empty((self.n_chains, 0))
        training["local_accs"] = jnp.empty((self.n_chains, 0))
        training["global_accs"] = jnp.empty((self.n_chains, 0))
        training["loss_vals"] = jnp.empty((0, self.n_epochs))

        production = {}
        production["chains"] = jnp.empty((self.n_chains, 0, self.n_dim))
        production["log_prob"] = jnp.empty((self.n_chains, 0))
        production["local_accs"] = jnp.empty((self.n_chains, 0))
        production["global_accs"] = jnp.empty((self.n_chains, 0))

        self.summary = {}
        self.summary['training'] = training
        self.summary['production'] = production

    def sample(self, initial_position):
        """
        Sample from the posterior using the local sampler.

        Args:
            initial_position (Device Array): Initial position.

        Returns:
            chains (Device Array): Samples from the posterior.
            nf_samples (Device Array): (n_nf_samples, n_dim)
            local_accs (Device Array): (n_chains, n_local_steps * n_loop)
            global_accs (Device Array): (n_chains, n_global_steps * n_loop)
            loss_vals (Device Array): (n_epoch * n_loop,)
        """

        # Note that auto-tune function needs to have the same number of steps
        # as the actual sampling loop to avoid recompilation.

        self.local_sampler_tuning(self.n_local_steps, initial_position)
        last_step = initial_position
        if self.use_global == True:
            last_step = self.global_sampler_tuning(last_step)

        last_step = self.production_run(last_step)

    def sampling_loop(self, initial_position, training=False):
        """
        Sampling loop for both the global sampler and the local sampler.

        Args:
            rng_keys_nf (Device Array): RNG keys for the normalizing flow global sampler.
            rng_keys_mcmc (Device Array): RNG keys for the local sampler.
            d_likelihood ?
            TODO: likelihood vs posterior?
            TODO: nf_samples - sometime int, sometimes samples

        """

        self.rng_keys_mcmc, positions, log_prob, local_acceptance, _ = self.local_sampler(
            self.rng_keys_mcmc, self.n_local_steps, initial_position, self.sampler_params
        )

        log_prob_output = np.copy(log_prob)

        if self.use_global == True:
            if training == True:
                if self.keep_quantile > 0:
                    max_log_prob = jnp.max(log_prob_output, axis=1)
                    cut = jnp.quantile(max_log_prob, self.keep_quantile)
                    cut_chains = positions[max_log_prob > cut]
                else:
                    cut_chains = positions
                chain_size = cut_chains.shape[0] * cut_chains.shape[1]
                if chain_size > self.max_samples:
                    flat_chain = cut_chains[
                        :, -int(self.max_samples / self.n_chains):
                    ].reshape(-1, self.n_dim)
                else:
                    flat_chain = cut_chains.reshape(-1, self.n_dim)

                variables = self.variables.unfreeze()
                variables["base_mean"] = jnp.mean(flat_chain, axis=0)
                variables["base_cov"] = jnp.cov(flat_chain.T)
                self.variables = flax.core.freeze(variables)

                flat_chain = (flat_chain - variables["base_mean"]) / jnp.sqrt(
                    jnp.diag(variables["base_cov"])
                )

                self.rng_keys_nf, self.state, loss_values = self.nf_training_loop(
                    self.rng_keys_nf,
                    self.state,
                    self.variables,
                    flat_chain,
                    self.n_epochs,
                    self.batch_size,
                )
                self.summary['training']['loss_vals'] = jnp.append(
                    self.summary['training']['loss_vals'], loss_values.reshape(1, -1), axis=0
                )

            (
                self.rng_keys_nf,
                nf_chain,
                log_prob,
                log_prob_nf,
                global_acceptance,
            ) = self.global_sampler(
                self.rng_keys_nf,
                self.n_global_steps,
                self.state.params,
                self.variables,
                self.likelihood_vec,
                positions[:, -1],
            )

            positions = jnp.concatenate((positions, nf_chain), axis=1)
            log_prob_output = jnp.concatenate(
                (log_prob_output, log_prob), axis=1)

        if training == True:
            self.summary['training']['chains'] = jnp.append(
                self.summary['training']['chains'], positions, axis=1
            )
            self.summary['training']['log_prob'] = jnp.append(
                self.summary['training']['log_prob'], log_prob_output, axis=1
            )
            self.summary['training']['local_accs'] = jnp.append(
                self.summary['training']['local_accs'], local_acceptance, axis=1
            )
            if self.use_global == True:
                self.summary['training']['global_accs'] = jnp.append(
                    self.summary['training']['global_accs'], global_acceptance, axis=1
                )
        else:
            self.summary['production']['chains'] = jnp.append(
                self.summary['production']['chains'], positions, axis=1
            )
            self.summary['production']['log_prob'] = jnp.append(
                self.summary['production']['log_prob'], log_prob_output, axis=1
            )
            self.summary['production']['local_accs'] = jnp.append(
                self.summary['production']['local_accs'], local_acceptance, axis=1
            )
            if self.use_global == True:
                self.summary['production']['global_accs'] = jnp.append(
                    self.summary['production']['global_accs'], global_acceptance, axis=1
                )
        last_step = positions[:, -1]

        return last_step

    def local_sampler_tuning(self, n_steps: int, initial_position: jnp.array, max_iter: int = 10):
        """
        Tuning the local sampler. This runs a number of iterations of the local sampler,
        and then uses the acceptance rate to adjust the local sampler parameters.
        Since this is mostly for a fast adaptation, we do not carry the sample state forward.
        Instead, we only adapt the sampler parameters using the initial position.

        Args:
            n_steps (int): Number of steps to run the local sampler.
            initial_position (Device Array): Initial position for the local sampler.
            max_iter (int): Number of iterations to run the local sampler.
        """
        if self.local_autotune is not None:
            print("Autotune found, start tuning sampler_params")
            self.sampler_params, self.local_sampler = self.local_autotune(
                self.local_sampler, self.rng_keys_mcmc, n_steps, initial_position, self.sampler_params, max_iter)
        else:
            print("No autotune found, use input sampler_params")

    def global_sampler_tuning(self, initial_position: jnp.ndarray):
        """
        Tuning the global sampler. This runs both the local sampler and the global sampler,
        and train the normalizing flow on the run.
        To adapt the normalizing flow, we need to keep certain amount of the data generated during the sampling.
        The data is stored in the summary dictionary.

        Args:
            initial_position (Device Array): Initial position for the sampler, shape (n_chains, n_dim)

        """
        print("Training normalizing flow")
        last_step = initial_position
        for _ in range(self.n_loop_training):
            last_step = self.sampling_loop(last_step, training=True)
        return last_step

    def production_run(self, initial_position):
        last_step = initial_position
        for _ in range(self.n_loop_production):
            self.sampling_loop(last_step)

    def get_sampler_state(self, training=False):
        if training == True:
            return self.summary['training']
        else:
            return self.summary['production']

    def sample_flow(self, n_samples):
        nf_samples = sample_nf(
            self.nf_model,
            self.state.params,
            self.rng_keys_nf,
            n_samples,
            self.variables,
        )
        return nf_samples

    def reset(self):
        training = {}
        training["chains"] = jnp.empty((self.n_chains, 0, self.n_dim))
        training["log_prob"] = jnp.empty((self.n_chains, 0))
        training["local_accs"] = jnp.empty((self.n_chains, 0))
        training["global_accs"] = jnp.empty((self.n_chains, 0))
        training["loss_vals"] = jnp.empty((0, self.n_epochs))

        production = {}
        production["chains"] = jnp.empty((self.n_chains, 0, self.n_dim))
        production["log_prob"] = jnp.empty((self.n_chains, 0))
        production["local_accs"] = jnp.empty((self.n_chains, 0))
        production["global_accs"] = jnp.empty((self.n_chains, 0))

        self.summary = {}
        self.summary['training'] = training
        self.summary['production'] = production
