# coding: utf-8

# import modules
import os
from functools import partial
import numpy as np
import tensorflow as tf
from keras.models import load_model
from keras.optimizers import RMSprop
from keras.callbacks import ReduceLROnPlateau, CSVLogger, EarlyStopping, ModelCheckpoint
from keras.applications import ResNet50
from keras.applications.resnet50 import preprocess_input
from maeshori.nlp_utils import create_word_dict
from maeshori.caps_utils import CocoGenerator
from maeshori.gen_utils import stack_batch
from maeshori.callbacks import IftttMakerWebHook
from ShowAndTell import ShowAndTell
from multi_gpu import make_parallel

# configs
embedding_dim = 512
lstm_units = 512
max_sentence_length = 64

print("### parameters")
print("embedding_dim: %s"%embedding_dim)
print("lstm_units: %s"%lstm_units)
print("max_sentence_length: %s"%max_sentence_length)


# configuration
img_channels = 3
img_rows = 224
img_cols = 224
num_classes = 1000
num_gpu = 8


# create resnet model instance
print("Loading image model")
#with tf.device("/gpu:1"):
img_model = ResNet50(weights='imagenet',
                 input_shape=(img_rows, img_cols, img_channels),
                 include_top=False, # without softmax layer (set True for training)
                 classes=num_classes)
img_model.trainable = False

img_feature_dim = img_model.output_shape[-1]

# caption preprocessor
def caps_preprocess(caption):
    import re
    return re.sub(r'\n|\.', '', caption.strip().lower())

# feature extractor
def deep_cnn_feature(img_data): # image (3-dim array) -> (feature_dim,)
    img_feature = img_model.predict(preprocess_input(img_data.astype(np.float32)))
    return img_feature.reshape((-1,))


# load MSCOCO
print("Loading MSCOCO")
# training data
coco_train = CocoGenerator('./COCO/', 'train2014',
                           word_dict_creator=partial(create_word_dict, idx_start_from=1),
                           caps_process=caps_preprocess, raw_img=False,
                           on_memory=True,
                           feature_extractor=deep_cnn_feature,
                           img_size=(img_rows, img_cols),
                           cache="./COCO/cache/resnet50_train.pkl")

# validation data
coco_val = CocoGenerator('./COCO/', 'val2014',
                         word_dict=coco_train.word_dict,
                         vocab_size=coco_train.vocab_size,
                         caps_process=caps_preprocess, raw_img=False,
                         on_memory=True,
                         feature_extractor=deep_cnn_feature,
                         img_size=(img_rows, img_cols),
                         cache='./COCO/cache/resnet50_val.pkl')


# load model
print("Preparing image captioning model")
with tf.device("/cpu:0"):
    im2txt_model = ShowAndTell(coco_train.vocab_size, img_feature_dim=img_feature_dim,
                               max_sentence_length=max_sentence_length)
    #im2txt_model = load_model('./results/model_weight/weights3_02-4.16_.hdf5', compile=False)

im2txt_model = make_parallel(im2txt_model, num_gpu)
im2txt_model.compile(loss="categorical_crossentropy", optimizer=RMSprop(lr=0.01), metrics=['acc'])

# callbacks
lr_reducer = ReduceLROnPlateau(factor=0.1, patience=1, min_lr=0.5e-6)
early_stopper = EarlyStopping(min_delta=0.001, patience=3)
checkpoint = ModelCheckpoint(filepath="./results/model_weight/weights2_{epoch:02d}-{val_loss:.2f}_.hdf5",
                             save_best_only=True)
csv_logger = CSVLogger('./results/logs/show_and_tell2.csv')
ifttt_url = 'https://maker.ifttt.com/trigger/keras_callback/with/key/' + os.environ['IFTTT_SECRET']
ifttt_notify = IftttMakerWebHook(ifttt_url)

# fit
print("Start Training")
coco_train_gen = coco_train.generator(img_size=(img_rows, img_cols),
                                      feature_extractor=deep_cnn_feature,
                                      maxlen=max_sentence_length, padding='post')
coco_train_gen = stack_batch(coco_train_gen, num_gpu)
coco_val_gen = coco_val.generator(img_size=(img_rows, img_cols),
                                  feature_extractor=deep_cnn_feature,
                                  maxlen=max_sentence_length, padding='post')
coco_val_gen = stack_batch(coco_val_gen, num_gpu)

im2txt_model.fit_generator(coco_train_gen,
                           steps_per_epoch=coco_train.num_captions // num_gpu,
                           epochs=100,
                           callbacks=[lr_reducer, early_stopper, csv_logger, checkpoint, ifttt_notify],
                           validation_data=coco_val_gen,
                           validation_steps=coco_val.num_captions // num_gpu,
                           max_q_size=1000,
                           verbose=0) # supress progress bar
