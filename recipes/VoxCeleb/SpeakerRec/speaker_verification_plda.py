#!/usr/bin/python3
"""Recipe for training a speaker verification system based on PLDA using the voxceleb dataset.
The system employs a pre-trained model followed by a PLDA transformation.
The pre-trained model is automatically downloaded from the web if not specified.

To run this recipe, run the following command:
    >  python speaker_verification_plda.py hyperparams/verification_plda_xvector.yaml

Authors
    * Nauman Dawalatabad 2020
    * Mirco Ravanelli 2020
"""

import os
import sys
import torch
import logging
import speechbrain as sb
import numpy
import pickle
from tqdm.contrib import tqdm
from speechbrain.utils.metric_stats import EER
from speechbrain.utils.data_utils import download_file
from speechbrain.data_io.data_io import convert_index_to_lab
from speechbrain.processing.PLDA_LDA import StatObject_SB
from speechbrain.processing.PLDA_LDA import Ndx
from speechbrain.processing.PLDA_LDA import fast_PLDA_scoring


def compute_embeddings(wavs, lens):
    """Definition of the steps for embedding computation from the waveforms"""
    with torch.no_grad():
        wavs = wavs.to(params["device"])
        feats = params["compute_features"](wavs)
        feats = params["mean_var_norm"](feats, lens)
        emb = params["embedding_model"](feats, lens=lens)
        emb = params["mean_var_norm_emb"](
            emb, torch.ones(emb.shape[0], device=params["device"])
        )
    return emb


def emb_computation_loop(split, set_loader, stat_file):
    """Computes the embeddings and saves the in a stat file"""
    # Extract embeddings (skip if already done)
    if not os.path.isfile(stat_file):
        embeddings = numpy.empty(
            shape=[0, params["emb_dim"]], dtype=numpy.float64
        )
        modelset = []
        segset = []
        with tqdm(set_loader, dynamic_ncols=True) as t:

            for wav in t:
                ids, wavs, lens = wav[0]
                mod = [x for x in ids]
                seg = [x for x in ids]
                modelset = modelset + mod
                segset = segset + seg

                # Enrolment and test embeddings
                embs = compute_embeddings(wavs, lens)
                xv = embs.squeeze().cpu().numpy()
                embeddings = numpy.concatenate((embeddings, xv), axis=0)

        modelset = numpy.array(modelset, dtype="|O")
        segset = numpy.array(segset, dtype="|O")

        # Intialize variables for start, stop and stat0
        s = numpy.array([None] * embeddings.shape[0])
        b = numpy.array([[1.0]] * embeddings.shape[0])

        # Stat object (used to collect embeddings)
        stat_obj = StatObject_SB(
            modelset=modelset,
            segset=segset,
            start=s,
            stop=s,
            stat0=b,
            stat1=embeddings,
        )
        logger.info(f"Saving stat obj for {split}")
        stat_obj.save_stat_object(stat_file)

    else:
        logger.info(f"Skipping embedding Extraction for {split}")
        logger.info(f"Loading previously saved stat_object for {split}")

        with open(stat_file, "rb") as input:
            stat_obj = pickle.load(input)

    return stat_obj


def compute_EER(scores_plda):
    """Computes the Equal Error Rate give the PLDA scores"""
    gt_file = os.path.join(params["data_folder"], "meta", "veri_test.txt")

    # Create ids, labels, and scoring list for EER evaluation
    ids = []
    labels = []
    positive_scores = []
    negative_scores = []
    for line in open(gt_file):
        lab = int(line.split(" ")[0].rstrip().split(".")[0].strip())
        enrol_id = line.split(" ")[1].rstrip().split(".")[0].strip()
        test_id = line.split(" ")[2].rstrip().split(".")[0].strip()

        # Assuming enrol_id and test_id are unique
        i = int(numpy.where(scores_plda.modelset == enrol_id)[0][0])
        j = int(numpy.where(scores_plda.segset == test_id)[0][0])

        s = float(scores_plda.scoremat[i, j])
        labels.append(lab)
        ids.append(enrol_id + "<>" + test_id)
        if lab == 1:
            positive_scores.append(s)
        else:
            negative_scores.append(s)

    # Clean variable
    del scores_plda

    # Final EER computation
    eer, th = EER(torch.tensor(positive_scores), torch.tensor(negative_scores))
    return eer


def download_and_pretrain():
    """Downaloads the pre-trained encoder and loads it"""
    save_model_path = params["output_folder"] + "/save/emb.ckpt"
    download_file(params["embedding_file"], save_model_path)
    params["embedding_model"].load_state_dict(
        torch.load(save_model_path), strict=True
    )


# Function to get mod and seg
def get_utt_ids_for_test(ids, data_dict):
    mod = [data_dict[x]["wav1"]["data"] for x in ids]
    seg = [data_dict[x]["wav2"]["data"] for x in ids]

    return mod, seg


if __name__ == "__main__":

    # Logger setup
    logger = logging.getLogger(__name__)
    current_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.append(os.path.dirname(current_dir))

    # Load hyperparameters file with command-line overrides
    params_file, overrides = sb.core.parse_arguments(sys.argv[1:])
    with open(params_file) as fin:
        params = sb.yaml.load_extended_yaml(fin, overrides)
    from voxceleb_prepare import prepare_voxceleb  # noqa E402

    # Create experiment directory
    sb.core.create_experiment_directory(
        experiment_directory=params["output_folder"],
        hyperparams_to_save=params_file,
        overrides=overrides,
    )

    # Prepare data from dev of Voxceleb1
    logger.info("Data preparation")
    prepare_voxceleb(
        data_folder=params["data_folder"],
        save_folder=params["save_folder"],
        splits=["train", "test"],
        split_ratio=[90, 10],
        seg_dur=300,
        rand_seed=params["seed"],
    )

    # Initialize PLDA vars
    modelset, segset = [], []
    embeddings = numpy.empty(shape=[0, params["emb_dim"]], dtype=numpy.float64)

    # Train set
    train_set = params["train_loader"]()
    train_set = train_set.get_dataloader()
    ind2lab = params["train_loader"].label_dict["spk_id"]["index2lab"]

    # Embedding file for train data
    xv_file = os.path.join(
        params["save_folder"], "VoxCeleb1_train_embeddings_stat_obj.pkl"
    )

    # Download models from the web if needed
    if "https://" in params["embedding_file"]:
        download_and_pretrain()
    else:
        params["embedding_model"].load_state_dict(
            torch.load(params["embedding_file"]), strict=True
        )

    # Put modules on the specified device
    params["compute_features"].to(params["device"])
    params["mean_var_norm"].to(params["device"])
    params["embedding_model"].to(params["device"])
    params["mean_var_norm_emb"].to(params["device"])

    # Switch encoder to eval modality
    params["embedding_model"].eval()

    # Computing training embeddigs (skip it of if already extracted)
    if not os.path.exists(xv_file):
        logger.info("Extracting embeddings from Training set..")
        with tqdm(train_set, dynamic_ncols=True) as t:
            for wav, spk_id in t:
                _, wav, lens = wav
                snt_id, spk_id, lens = spk_id

                # For modelset
                spk_id_str = convert_index_to_lab(spk_id, ind2lab)

                # Flattening speaker ids
                spk_ids = [sid[0] for sid in spk_id_str]
                modelset = modelset + spk_ids

                # For segset
                segset = segset + snt_id

                # Compute embeddings
                emb = compute_embeddings(wav, lens)
                xv = emb.squeeze(1).cpu().numpy()
                embeddings = numpy.concatenate((embeddings, xv), axis=0)

        # Speaker IDs and utterance IDs
        modelset = numpy.array(modelset, dtype="|O")
        segset = numpy.array(segset, dtype="|O")

        # Intialize variables for start, stop and stat0
        s = numpy.array([None] * embeddings.shape[0])
        b = numpy.array([[1.0]] * embeddings.shape[0])

        embeddings_stat = StatObject_SB(
            modelset=modelset,
            segset=segset,
            start=s,
            stop=s,
            stat0=b,
            stat1=embeddings,
        )

        del embeddings

        # Save TRAINING embeddings in StatObject_SB object
        embeddings_stat.save_stat_object(xv_file)

    else:
        # Load the saved stat object for train embedding
        logger.info("Skipping embedding Extraction for training set")
        logger.info(
            "Loading previously saved stat_object for train embeddings.."
        )
        with open(xv_file, "rb") as input:
            embeddings_stat = pickle.load(input)

    # Training Gaussian PLDA model
    logger.info("Training PLDA model")
    params["compute_plda"].plda(embeddings_stat)
    logger.info("PLDA training completed")

    # Set paths for enrol/test  embeddings
    enrol_stat_file = os.path.join(params["save_folder"], "stat_enrol.pkl")
    test_stat_file = os.path.join(params["save_folder"], "stat_test.pkl")
    ndx_file = os.path.join(params["save_folder"], "ndx.pkl")

    # Data loader
    enrol_set = params["enrol_loader"]()
    enrol_set = enrol_set.get_dataloader()
    test_set = params["test_loader"]()
    test_set = test_set.get_dataloader()

    # Compute enrol and Test embeddings
    enrol_obj = emb_computation_loop("enrol", enrol_set, enrol_stat_file)
    test_obj = emb_computation_loop("test", test_set, test_stat_file)

    # Prepare Ndx Object
    if not os.path.isfile(ndx_file):
        models = enrol_obj.modelset
        testsegs = test_obj.modelset

        logger.info("Preparing Ndx")
        ndx_obj = Ndx(models=models, testsegs=testsegs)
        logger.info("Saving ndx obj...")
        ndx_obj.save_ndx_object(ndx_file)
    else:
        logger.info("Skipping Ndx preparation")
        logger.info("Loading Ndx from disk")
        with open(ndx_file, "rb") as input:
            ndx_obj = pickle.load(input)

    # PLDA scoring
    logger.info("PLDA scoring...")
    scores_plda = fast_PLDA_scoring(
        enrol_obj,
        test_obj,
        ndx_obj,
        params["compute_plda"].mean,
        params["compute_plda"].F,
        params["compute_plda"].Sigma,
    )

    logger.info("Computing EER... ")

    # Cleaning variable
    del enrol_set
    del test_set
    del enrol_obj
    del test_obj
    del embeddings_stat

    # Final EER computation
    eer = compute_EER(scores_plda)
    logger.info("EER=%f", eer)