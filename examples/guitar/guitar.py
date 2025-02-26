import logging
import os
import numpy as np
import tensorflow as tf
import scipy
import matplotlib.pyplot as plt
import h5py

from aphin.utils.data.dataset import SynRMDataset, PHIdentifiedDataset, Dataset
from aphin.utils.configuration import Configuration
import aphin.utils.visualizations as aphin_vis
from aphin.utils.callbacks_tensorflow import callbacks
from aphin.identification import APHIN
from aphin.layers.phq_layer import PHQLayer, PHLayer
from aphin.utils.save_results import (
    save_weights,
    write_to_experiment_overview,
    save_evaluation_times,
    save_training_times,
)
from aphin.utils.print_matrices import print_matrices


def main(
    config_path_to_file=None,
):  # {None} if no config file shall be loaded, else create str with path to config file
    logging.info(f"Loading configuration")
    # Priority 1: config_path_to_file (input of main function)
    # Priority 2: manual_results_folder (below)
    # -> variable is written to config_info which is interpreted as follows (see also doc of Configuration class):
    # config_info:          -{None}                 use default config.yml that should be on the working_dir level
    #                                                 -> config for identifaction
    #                                                 -> if load_network -> load config and weights from default path
    #                         -config_filename.yml    absolute path to config file ending with .yml
    #                                                 -> config for identifaction
    #                                                 -> if load_network -> load config and weights from .yml path
    #                         -/folder/name/          absolute path of directory that includes a config.yml and .weights.h5
    #                                                 -> config for loading results
    #                         -result_folder_name     searches for a subfolder with result_folder_name under working dir that
    #                                                 includes a config.yml and .weights.h5
    #                                                 -> config for loading results
    manual_results_folder = None  # {None} if no results shall be loaded, else create str with folder name or path to results folder

    # write to config_info
    if config_path_to_file is not None:
        config_info = config_path_to_file
    elif manual_results_folder is not None:
        config_info = manual_results_folder
    else:
        config_info = None

    # setup experiment based on config file
    working_dir = os.path.dirname(__file__)
    configuration = Configuration(working_dir, config_info)
    cfg = configuration.cfg_dict
    data_dir, log_dir, weight_dir, result_dir = configuration.directories

    data = np.load("guitar.npz")

    # # load mat file using h5py
    # data = h5py.File(cfg["matfile_path"], "r")["snapshotData"]["trajectory"]
    # t = h5py.File(cfg["matfile_path"], "r")["snapshotData"]["same4all"]["t"][:]
    # freq = data["freq"][:]
    # x =  np.array([data[data["x"][i, 0]][:] for i in range(100)])
    #
    # # save data as compressed numpy file
    # np.savez_compressed("guitar.npz", x=x, t=t, freq=freq)
    t = data["t"]
    u = np.array([-np.sin(2*np.pi*freq_*t) for freq_ in data["freq"]])
    x = data["x"]
    dt = (t[1] - t[0])[0]
    # numerical differentiation
    x_dt = np.gradient(x, dt, axis=1)
    x = x[:, : , np.newaxis]
    x_dt = x_dt[:, : , np.newaxis]

    data = Dataset(t=t, X=x, X_dt=x_dt, U=u)

    aphin_vis.setup_matplotlib(cfg["setup_matplotlib"])

    # filter data with savgol filter
    if cfg["filter_data"]:
        logging.info("Data is filtered")
        data.filter_data(interp_equidis_t=False)
    else:
        logging.info("Data is not filtered.")

    # reduced size
    r = cfg["r"]

    # train-test split
    data.train_test_split(0.6, seed=cfg["seed"])

    data.scale_X(
        scaling_values=cfg["scaling_values"], domain_split_vals=cfg["domain_split_vals"]
    )

    # scale u manually
    data.reshape_inputs_to_features()

    # transform to feature form that is used by the deep learning
    data.states_to_features()
    t, x, dx_dt, u, mu = data.data

    # %% Create APHIN
    logging.info(
        "################################   2. Model      ################################"
    )
    validation = True
    if validation:
        monitor = "val_loss"
    else:
        monitor = "loss"

    callback = callbacks(
        weight_dir,
        tensorboard=cfg["tensorboard"],
        log_dir=log_dir,
        monitor=monitor,
        earlystopping=False,
        patience=500,
    )

    n_sim, n_t, n_n, n_dn, n_u, n_mu = data.shape
    regularizer = tf.keras.regularizers.L1L2(l1=cfg["l1"], l2=cfg["l2"])
    system_layer = PHQLayer(
        r,
        n_u=n_u,
        n_mu=n_mu,
        name="phq_layer",
        layer_sizes=cfg["layer_sizes_ph"],
        activation=cfg["activation_ph"],
        regularizer=regularizer,
        # dtype=tf.float64,
    )

    aphin = APHIN(
        r,
        x=x,
        u=u,
        system_layer=system_layer,
        layer_sizes=cfg["layer_sizes_ae"],
        activation=cfg["activation_ae"],
        l_rec=cfg["l_rec"],
        l_dz=cfg["l_dz"],
        l_dx=cfg["l_dx"],
        use_pca=cfg["use_pca"],
        pca_only=cfg["pca_only"],
        pca_order=cfg["n_pca"],
        pca_scaling=cfg["pca_scaling"],
        # dtype=tf.float64,
    )

    aphin.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=cfg["lr"]),
        loss=tf.keras.losses.MSE,
    )
    aphin.build(input_shape=([x.shape, dx_dt.shape, u.shape], None))

    # Fit or learn neural network
    if cfg["load_network"]:
        logging.info(f"Loading NN weights.")
        aphin.load_weights(os.path.join(weight_dir, ".weights.h5"))
    else:
        if cfg["prelearn_rec"]:
            logging.info(f"Loading NN weights from only rec learning.")
            aphin.load_weights(os.path.join(result_dir, ".weights.h5"))
        logging.info(f"Fitting NN weights.")
        n_train = int(0.8 * x.shape[0])
        x_train = [x[:n_train], dx_dt[:n_train], u[:n_train]]
        x_val = [x[n_train:], dx_dt[n_train:], u[n_train:]]
        train_hist = aphin.fit(
            x=x_train,
            validation_data=x_val,
            epochs=cfg["n_epochs"],
            batch_size=cfg["batch_size"],
            verbose=2,
            callbacks=callback,
        )
        save_training_times(train_hist, result_dir)
        aphin_vis.plot_train_history(
            train_hist, save_path=result_dir, validation=validation
        )

        # load best weights
        aphin.load_weights(os.path.join(weight_dir, ".weights.h5"))

    # write data to results directory
    save_weights(weight_dir, result_dir, load_network=cfg["load_network"])
    write_to_experiment_overview(
        cfg, result_dir, load_network=cfg["load_network"]
    )

    # %% Validation
    logging.info(
        "################################   3. Validation ################################"
    )
    t_test, x_test, dx_dt_test, u_test, mu_test = data.test_data
    n_sim_test, n_t_test, _, _, _, _ = data.shape_test
    print_matrices(system_layer, mu=mu_test, n_t=n_t_test)

    # calculate projection and Jacobian errors
    # file_name = "projection_error.txt"
    # projection_error_file_dir = os.path.join(result_dir, file_name)
    # aphin.get_projection_properties(x, x_test, file_dir=projection_error_file_dir)

    # %% Validation of the AE reconstruction
    # get original quantities
    data_id = PHIdentifiedDataset.from_identification(
        data, system_layer, aphin, integrator_type="imr"
    )
    save_evaluation_times(data_id, result_dir)

    # reproject
    # num_rand_pick_entries = 1000
    # data.reproject_with_basis(
    #     [V, V],
    #     idx=[slice(80, 100), slice(105, 125)],
    #     pick_method="rand",
    #     pick_entry=num_rand_pick_entries,
    #     seed=cfg["seed"],
    # )
    # data_id.reproject_with_basis(
    #     [V, V],
    #     idx=[slice(80, 100), slice(105, 125)],
    #     pick_method="rand",
    #     pick_entry=num_rand_pick_entries,
    #     seed=cfg["seed"],
    # )

    # domain_split_vals_projected = [
    #     3,
    #     72,
    #     5,
    #     num_rand_pick_entries,
    #     5,
    #     num_rand_pick_entries,
    # ]

    # data.calculate_errors(
    #     data_id,
    #     domain_split_vals=domain_split_vals_projected,
    #     save_to_txt=True,
    #     result_dir=result_dir,
    # )

    # %% calculate errors
    data.calculate_errors(
        data_id,
        domain_split_vals=cfg["domain_split_vals"],
        save_to_txt=True,
        result_dir=result_dir,
    )
    aphin_vis.plot_errors(
        data,
        t=data.t_test,
        save_name=os.path.join(result_dir, "rms_error"),
        domain_names=cfg["domain_names"],
        save_to_csv=True,
        yscale="log",
    )

    # %% plot trajectories
    use_train_data = True
    use_rand = True
    if use_rand:
        idx_gen = "rand"
        idx_custom_tuple = None
    else:
        # use predefined indices
        idx_gen = "custom"
        # idx_eta = np.arange(3)
        # idx_phi = np.arange(3, 75)
        # idx_rigid = np.arange(75, 80)
        # idx_elastic_modes = np.arange(80, 100)
        # idx_Drigid = np.arange(100, 105)
        # idx_Delastic = np.arange(105, 125)
        # all domains
        # idx_n_n = np.array([0] * 7)
        # idx_n_dn = np.array([0, 3, 4, 75, 80, 101, 106])
        # no velocities
        idx_n_n = np.array([0] * 5)
        idx_n_dn = np.array([0, 3, 4, 75, 80])
        idx_n_f = np.array([0, 4, 13, 20, 25])  # for latent space
        idx_custom_tuple = [
            (idx_n_n[i], idx_n_dn[i], idx_n_f[i]) for i in range(idx_n_n.shape[0])
        ]
    aphin_vis.plot_time_trajectories_all(
        data,
        data_id,
        use_train_data=use_train_data,
        idx_gen=idx_gen,
        result_dir=result_dir,
        idx_custom_tuple=idx_custom_tuple,
    )

    aphin_vis.plot_u(data=data, use_train_data=use_train_data)

    # avoid that the script stops and keep the plots open
    plt.show()

    print("debug")


if __name__ == "__main__":
    main()
