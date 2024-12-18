import numpy as np
from tqdm import tqdm
import math

import tensorflow as tf
from tensorflow import keras

from .autoencoder_kl import Decoder, Encoder
from .diffusion_model import UNetModel
from .clip_encoder import CLIPTextTransformer
from .clip_tokenizer import SimpleTokenizer
from .constants import _UNCONDITIONAL_TOKENS, _ALPHAS_CUMPROD
from PIL import Image

MAX_TEXT_LEN = 77

# https://github.com/divamgupta/stable-diffusion-tensorflow

class StableDiffusion:
    def __init__(self, img_height=1000, img_width=1000, jit_compile=False, download_weights=True):
        self.img_height = img_height
        self.img_width = img_width
        self.tokenizer = SimpleTokenizer()

        text_encoder, diffusion_model, decoder, encoder = get_models(img_height, img_width, download_weights=download_weights)
        self.text_encoder = text_encoder
        self.diffusion_model = diffusion_model
        self.decoder = decoder
        self.encoder = encoder

        if jit_compile:
            self.text_encoder.compile(jit_compile=True)
            self.diffusion_model.compile(jit_compile=True)
            self.decoder.compile(jit_compile=True)
            self.encoder.compile(jit_compile=True)

        self.dtype = tf.float32
        if tf.keras.mixed_precision.global_policy().name == 'mixed_float16':
            self.dtype = tf.float16
            
    def encode(self, input_image):
        return self.encoder(input_image)
    
    def decode(self, encoded):
        decoded = self.decoder.predict_on_batch(encoded)
        decoded = ((decoded + 1) / 2) * 255
        return np.clip(decoded, 0, 255).astype("uint8")[0,:,:,:]
    
    def text_encode(self, prompt):
        inputs = self.tokenizer.encode(prompt)
        phrase = inputs + [49407] * (77 - len(inputs))
        phrase = np.array(phrase)[None].astype("int32")
        
        # Encode prompt tokens (and their positions) into a "context vector"
        pos_ids = np.array(list(range(77)))[None].astype("int32")
        context = self.text_encoder.predict_on_batch([phrase, pos_ids])
        return inputs, context

    def context_from_inputs(self, inputs):
        phrase = inputs + [49407] * (77 - len(inputs))
        phrase = np.array(phrase)[None].astype("int32")
             
        # Encode prompt tokens (and their positions) into a "context vector"
        pos_ids = np.array(list(range(77)))[None].astype("int32")
        return self.text_encoder.predict_on_batch([phrase, pos_ids])
    
    def tokenizer_decode(self, inputs):
        # tokens to text
        return self.tokenizer.decode(inputs);
    
    def generate_from_seed(
        self,
        prompt,
        negative_prompt=None,
        batch_size=1,
        num_steps=25,
        unconditional_guidance_scale=7.5,
        temperature=1,
        seed=None,
        input_image=None,
        input_mask=None,
        input_image_strength=0.5,
        feedback = False,
        use_auto_mask=False
    ):
        singles = False
        if batch_size == 0:
            batch_size = 1
            singles = True
             
        tf.random.set_seed(seed)
            
        # Tokenize prompt (i.e. starting context)
        inputs = self.tokenizer.encode(prompt)
        assert len(inputs) < 77, "Prompt is too long (should be < 77 tokens)"
        phrase = inputs + [49407] * (77 - len(inputs))
        phrase = np.array(phrase)[None].astype("int32")
        phrase = np.repeat(phrase, batch_size, axis=0)

        # Encode prompt tokens (and their positions) into a "context vector"
        pos_ids = np.array(list(range(77)))[None].astype("int32")
        pos_ids = np.repeat(pos_ids, batch_size, axis=0)
        context = self.text_encoder.predict_on_batch([phrase, pos_ids])
        
        input_image_tensor = None
        input_image_array = None
        if type(input_image) is str:
            input_image = Image.open(input_image)
            input_image = input_image.resize((self.img_width, self.img_height))
            input_image_array = np.array(input_image, dtype=np.float32)[None,...,:3]
            #print("generate_from_seed:input_image_array shape", input_image_array.shape)

            input_image_tensor = tf.cast((input_image_array / 255.0) * 2 - 1, self.dtype)
            #print("generate_from_seed:input_image_tensor shape", input_image_tensor.shape)

        input_mask_array = None
        if type(input_mask) is str:
            input_mask = Image.open(input_mask)
            input_mask = input_mask.resize((self.img_width, self.img_height))
            input_mask_array = np.array(input_mask, dtype=np.float32)[None,...,:3]
            input_mask_array =  input_mask_array / 255.0
            #print("input_mask_array shape", input_mask_array.shape)
            
            #latent_mask = input_mask.resize((self.img_width//8, self.img_height//8))
            #latent_mask = np.array(latent_mask, dtype=np.float32)[None,...,None]
            #latent_mask = 1 - (latent_mask.astype("float") / 255.0)
            #print("latent_mask shape", latent_mask.shape)
            #latent_mask_tensor = tf.cast(tf.repeat(latent_mask, batch_size , axis=0), self.dtype)
            #print("latent_mask_tensor.shape", latent_mask_tensor.shape)    # latent_mask_tensor.shape (1, 64, 64, 3, 1)
            

        # Tokenize negative prompt or use default padding tokens
        unconditional_tokens = _UNCONDITIONAL_TOKENS
        if negative_prompt is not None:
            inputs = self.tokenizer.encode(negative_prompt)
            assert len(inputs) < 77, "Negative prompt is too long (should be < 77 tokens)"
            unconditional_tokens = inputs + [49407] * (77 - len(inputs))

        # Encode unconditional tokens (and their positions) into an
        # "unconditional context vector"
        unconditional_tokens = np.array(unconditional_tokens)[None].astype("int32")
        unconditional_tokens = np.repeat(unconditional_tokens, batch_size, axis=0)
        self.unconditional_tokens = tf.convert_to_tensor(unconditional_tokens)
        unconditional_context = self.text_encoder.predict_on_batch(
            [self.unconditional_tokens, pos_ids]
        )
        
        # Return evenly spaced values within a given interval
        timesteps = np.arange(1, 1000, 1000 // num_steps)
        idx_time = min(len(timesteps)-1, int(len(timesteps)*input_image_strength*temperature))
        input_img_noise_t = timesteps[ idx_time ]
        latent, alphas, alphas_prev = self.get_starting_parameters(
            timesteps, batch_size, seed , input_image=input_image_tensor, input_img_noise_t=input_img_noise_t
        )
        
        #print("latent shape", latent.shape)

        if input_image is not None:
            idx_time = min(len(timesteps)-1, int(len(timesteps)*input_image_strength))
            timesteps = timesteps[: idx_time]

        #print(num_steps, idx_time, timesteps[idx_time])
        #print(timesteps)
        
        # Diffusion stage
        latent_orgin = None
        mix = None
        latent_mix =  None
        out_list = []
        progbar = tqdm(list(enumerate(timesteps))[::-1])
        for index, timestep in progbar:
            progbar.set_description(f"{index:3d} {timestep:3d}")
            
            if latent_mix is not None:
                latent = latent_mix
                
            e_t = self.get_model_output(
                latent,
                timestep,
                context,
                unconditional_context,
                unconditional_guidance_scale,
                batch_size,
            )
            
            a_t, a_prev = alphas[index], alphas_prev[index]
            
            latent, pred_x0 = self.get_x_prev_and_pred_x0(
                latent, e_t, index, a_t, a_prev)#, temperature, seed)

            if input_mask is not None and input_image is not None:
                # If mask is provided, noise at current timestep will be added to input image.
                # The intermediate latent will be merged with input latent.
                latent_orgin, alphas, alphas_prev = self.get_starting_parameters(
                    timesteps, batch_size, seed , input_image=input_image_tensor, input_img_noise_t=timestep
                )#############'''
                
                #print("latent_orgin shape", latent_orgin.shape)
                #latent = latent_orgin * latent_mask_tensor + latent * (1 - latent_mask_tensor)
                
                latent_decoded = self.decoder.predict_on_batch(latent)
                latent_orgin_decoded = self.decoder.predict_on_batch(latent_orgin)
                
                # Feedback
                if feedback:
                    mix = latent_orgin_decoded * (1 - input_mask_array) + latent_decoded * (input_mask_array)
                    latent_mix =  self.encoder(mix)
            
            if singles:
                decoded = self.decode_latent(latent)#, input_image_array, input_mask_array)
                out_list.append((decoded[0,:,:,:], "latent"))
                
                if input_mask is not None and input_image is not None:
                    decoded = self.decode_latent(latent, input_image_array, input_mask_array)
                    out_list.append((decoded[0,:,:,:], "latent masked"))
                
                s='''if latent_orgin is not None:
                    decoded = self.decode_latent(latent_orgin)#, input_image_array, input_mask_array)
                    out_list.append((decoded[0,:,:,:], "latent_orgin"))#################'''
                    
                s='''if mix is not None:
                    mix = ((mix + 1) / 2) * 255            
                    mix = np.clip(mix, 0, 255)[0,:,:,:].astype("uint8")
                    out_list.append((mix, "mix"))################'''
                
        if singles:
            out_list.append((decoded[0,:,:,:], ""))
        else:
            if feedback:
                decoded = self.decode_latent(latent)
                out_list.append((decoded[0,:,:,:], ""))
            else:
                decoded = self.decode_latent(latent, input_image_array, input_mask_array, use_auto_mask)
                for i in range(decoded.shape[0]):
                    out_list.append((decoded[i,:,:,:], ""))
                       
        return out_list
    
    def get_latent(self, input_image=None):
        input_image_tensor = None
        input_image_array = None
        latent = None
        if type(input_image) is str:
            input_image = Image.open(input_image)
            input_image = input_image.resize((self.img_width, self.img_height))
            input_image_array = np.array(input_image, dtype=np.float32)[None,...,:3]
            input_image_tensor = tf.cast((input_image_array / 255.0) * 2 - 1, self.dtype)
            latent = self.encoder(input_image_tensor) 
            
        return latent
        
    def get_noise_latent(
        self, 
        seed,
        input_image=None
    ):
        tf.random.set_seed(seed)
        n_h = self.img_height // 8
        n_w = self.img_width // 8
        
        input_image_tensor = None
        input_image_array = None
        latent = None
        if type(input_image) is str:
            input_image = Image.open(input_image)
            input_image = input_image.resize((self.img_width, self.img_height))
            input_image_array = np.array(input_image, dtype=np.float32)[None,...,:3]
            #print("get_noise_latent:input_image_array shape", input_image_array.shape)

            input_image_tensor = tf.cast((input_image_array / 255.0) * 2 - 1, self.dtype)
            #print("get_noise_latent:input_image_tensor shape", input_image_tensor.shape)
            latent = self.encoder(input_image_tensor)    
            
        return tf.random.normal((1, n_h, n_w, 4), seed=seed), latent
    
    def get_noisy_img(
        self,
        num_steps=25,
        temperature=1,
        seed=None,
        input_image=None,
        input_image_strength=0.5,
    ):
        
        tf.random.set_seed(seed)
        
        input_image_tensor = None
        input_image_array = None
        if type(input_image) is str:
            input_image = Image.open(input_image)
            input_image = input_image.resize((self.img_width, self.img_height))
            input_image_array = np.array(input_image, dtype=np.float32)[None,...,:3]
            #print("input_image_array shape", input_image_array.shape)

            input_image_tensor = tf.cast((input_image_array / 255.0) * 2 - 1, self.dtype)
            #print("input_image_tensor shape", input_image_tensor.shape)         
        
        # Return evenly spaced values within a given interval
        timesteps = np.arange(1, 1000, 1000 // num_steps)
        idx_time = min(len(timesteps)-1, int(len(timesteps)*input_image_strength*temperature))
        input_img_noise_t = timesteps[idx_time]
        #print(num_steps, idx_time, input_img_noise_t)
        #print(timesteps)
        latent, alphas, alphas_prev = self.get_starting_parameters(
            timesteps, 1, seed , input_image=input_image_tensor, input_img_noise_t=input_img_noise_t
        )
        
        return latent
    
    def add_noise_latent(
        self,
        noise_block,
        latent=None,
        num_steps=25,
        temperature=1,
        input_image_strength=0.5,
    ):      
        if latent is None:
            return noise_block
        
        # Return evenly spaced values within a given interval
        timesteps = np.arange(1, 1000, 1000 // num_steps)
        idx_time = min(len(timesteps)-1, int(len(timesteps)*input_image_strength*temperature))
        input_img_noise_t = timesteps[ idx_time ]
        
        return self.add_noise(latent, input_img_noise_t, noise_block)
    
    def generate_from_latent_noise(
        self,
        latent_noise,
        prompt,
        negative_prompt=None,
        num_steps=25,
        unconditional_guidance_scale=7.5,
        input_image_strength=1,
        use_auto_mask=False
    ):
        
        context, unconditional_context = self.tokenize(
            prompt, 
            negative_prompt,
            num_steps
        )
        
        return self.diffuse(
            latent_noise, 
            context,
            unconditional_context,
            num_steps, 
            unconditional_guidance_scale, 
            input_image_strength,
            use_auto_mask
        )
    
        s='''return self.tokenize_diffuse(
            latent_noise, 
            prompt, 
            negative_prompt=negative_prompt, 
            num_steps=num_steps,
            unconditional_guidance_scale=unconditional_guidance_scale,
            input_image_strength=input_image_strength,
            use_auto_mask=use_auto_mask
        )#'''
        
    def generate_from_noise_img(
        self,
        prompt,
        negative_prompt=None,
        num_steps=25,
        unconditional_guidance_scale=7.5,
        noise_img_block = None,
        input_image_strength=1,
        use_auto_mask=False
    ):   
        batch_size = 1
        seed = 1
        
        # Return evenly spaced values within a given interval
        timesteps = np.arange(1, 1000, 1000 // num_steps)
        latent, alphas, alphas_prev = self.get_starting_parameters(
            timesteps, batch_size, seed , noise=noise_img_block
        )
        
        context, unconditional_context = self.tokenize(
            prompt, 
            negative_prompt,
            num_steps
        )
        
        return self.diffuse(
            latent, 
            context,
            unconditional_context,
            num_steps, 
            unconditional_guidance_scale, 
            input_image_strength,
            use_auto_mask
        )
    
        s='''return self.tokenize_diffuse(
            latent, 
            prompt, 
            negative_prompt=negative_prompt, 
            num_steps=num_steps,
            unconditional_guidance_scale=unconditional_guidance_scale,
            input_image_strength=input_image_strength,
            use_auto_mask=use_auto_mask
        )#'''
       
    def generate_from_context(
        self, 
        context,
        unconditional_context,
        num_steps=25,
        unconditional_guidance_scale=7.5,
        noise_img_block = None,
        input_image_strength=1,
        use_auto_mask=False
    ):   
        batch_size = 1
        seed = 1
        
        # Return evenly spaced values within a given interval
        timesteps = np.arange(1, 1000, 1000 // num_steps)
        latent, alphas, alphas_prev = self.get_starting_parameters(
            timesteps, batch_size, seed , noise=noise_img_block
        )
        
        return self.diffuse(
            latent, 
            context,
            unconditional_context,
            num_steps, 
            unconditional_guidance_scale, 
            input_image_strength,
            use_auto_mask
        ) 
        
    def tokenize(
        self,
        prompt,
        negative_prompt=None
    ):            
        # Tokenize prompt (i.e. starting context)
        inputs = self.tokenizer.encode(prompt)
        assert len(inputs) < 77, "Prompt is too long (should be < 77 tokens)"
        phrase = inputs + [49407] * (77 - len(inputs))
        phrase = np.array(phrase)[None].astype("int32")
        #phrase = np.repeat(phrase, batch_size, axis=0)

        # Encode prompt tokens (and their positions) into a "context vector"
        pos_ids = np.array(list(range(77)))[None].astype("int32")
        #pos_ids = np.repeat(pos_ids, batch_size, axis=0)
        context = self.text_encoder.predict_on_batch([phrase, pos_ids])
        
        # Tokenize negative prompt or use default padding tokens
        unconditional_tokens = _UNCONDITIONAL_TOKENS
        if negative_prompt is not None:
            inputs = self.tokenizer.encode(negative_prompt)
            assert len(inputs) < 77, "Negative prompt is too long (should be < 77 tokens)"
            unconditional_tokens = inputs + [49407] * (77 - len(inputs))

        # Encode unconditional tokens (and their positions) into an
        # "unconditional context vector"
        unconditional_tokens = np.array(unconditional_tokens)[None].astype("int32")
        #unconditional_tokens = np.repeat(unconditional_tokens, batch_size, axis=0)
        self.unconditional_tokens = tf.convert_to_tensor(unconditional_tokens)
        unconditional_context = self.text_encoder.predict_on_batch(
            [self.unconditional_tokens, pos_ids]
        )
        
        return context, unconditional_context
    
    def diffuse(
        self,
        latent,
        context,
        unconditional_context,
        num_steps=25,
        unconditional_guidance_scale=7.5,
        input_image_strength=1,
        use_auto_mask=False
    ):
        batch_size = 1
        
        timesteps = np.arange(1, 1000, 1000 // num_steps)
        alphas = [_ALPHAS_CUMPROD[t] for t in timesteps]    # _ALPHAS_CUMPROD[0] = .99915, _ALPHAS_CUMPROD[999] = .00466
        alphas_prev = [1.0] + alphas[:-1]
        idx_time = min(len(timesteps)-1, int(len(timesteps)*input_image_strength))
        #print(num_steps, idx_time, timesteps[idx_time])
        #timesteps = timesteps[: idx_time]
        #print(timesteps)
        
        input_image_tensor = None
        input_image_array = None

        input_mask_array = None
        input_mask=None
        
        # Diffusion stage
        latent_orgin = None
        mix = None
        latent_mix =  None
        out_list = []
        progbar = tqdm(list(enumerate(timesteps))[::-1])
        for index, timestep in progbar:
            progbar.set_description(f"{index:3d} {timestep:3d}")
            
            if latent_mix is not None:
                latent = latent_mix
                
            e_t = self.get_model_output(
                latent,
                timestep,
                context,
                unconditional_context,
                unconditional_guidance_scale,
                batch_size,
            )
            
            a_t, a_prev = alphas[index], alphas_prev[index]
            
            latent, pred_x0 = self.get_x_prev_and_pred_x0(
                latent, e_t, index, a_t, a_prev)

        decoded = self.decode_latent(latent, input_image_array, input_mask_array, use_auto_mask)
        out_list.append((decoded[0,:,:,:], ""))
                       
        return out_list
    
    def decode_latent(self, latent, input_image_array=None, input_mask_array=None, use_auto_mask=False):
        # Decoding stage
        decoded = self.decoder.predict_on_batch(latent)
        #print("type(decoded)", type(decoded))   # type(decoded) <class 'numpy.ndarray'>
        #print("decoded.shape", decoded.shape)   # decoded.shape (1, 512, 896, 3)
        decoded = ((decoded + 1) / 2)
        auto_mask = None
        if use_auto_mask:
            auto_mask = np.clip((decoded - .25) / .5, 0, 1)
        decoded = decoded * 255

        if (input_image_array is not None) and (input_mask_array is not None):
          # Merge inpainting output with original image
          #print("type(input_mask_array)", type(input_mask_array))   # type(input_mask_array) <class 'numpy.ndarray'>
          #print("input_mask_array.shape", input_mask_array.shape)   # input_mask_array.shape (1, 512, 896, 3)
            if use_auto_mask:
                mask = np.minimum(auto_mask, input_mask_array)
                decoded = input_image_array * (mask) + np.array(decoded) * (1 - mask)
            else:
                decoded = input_image_array * (input_mask_array) + np.array(decoded) * (1 - input_mask_array)
        elif use_auto_mask:
            decoded = input_image_array * (auto_mask) + np.array(decoded) * (1 - auto_mask)
            
        return np.clip(decoded, 0, 255).astype("uint8")

    def timestep_embedding(self, timesteps, dim=320, max_period=10000):
        half = dim // 2
        freqs = np.exp(
            -math.log(max_period) * np.arange(0, half, dtype="float32") / half
        )
        args = np.array(timesteps) * freqs
        embedding = np.concatenate([np.cos(args), np.sin(args)])
        return tf.convert_to_tensor(embedding.reshape(1, -1),dtype=self.dtype)

    def add_noise(self, latent , t , noise = None):
        batch_size,w,h = latent.shape[0] , latent.shape[1] , latent.shape[2]
        if noise is None:
            noise = tf.random.normal((batch_size,w,h,4), dtype=self.dtype)
        # _ALPHAS_CUMPROD[0] = .99915, _ALPHAS_CUMPROD[999] = .00466
        sqrt_alpha_prod = _ALPHAS_CUMPROD[t] ** 0.5
        sqrt_one_minus_alpha_prod = (1 - _ALPHAS_CUMPROD[t]) ** 0.5
        return  sqrt_alpha_prod * latent + sqrt_one_minus_alpha_prod * noise

    def get_starting_parameters(self, timesteps, batch_size, seed, input_image=None, input_img_noise_t=None, noise = None):
        n_h = self.img_height // 8
        n_w = self.img_width // 8
        alphas = [_ALPHAS_CUMPROD[t] for t in timesteps]    # _ALPHAS_CUMPROD[0] = .99915, _ALPHAS_CUMPROD[999] = .00466
        alphas_prev = [1.0] + alphas[:-1]
        if input_image is None:
            if noise is None:
                latent = tf.random.normal((batch_size, n_h, n_w, 4), seed=seed)
            else:
                latent = noise
        else:
            # input_image is -1 to 1
            #print("get_starting_parameters:input_image shape", input_image.shape)
            latent = self.encoder(input_image)
            #print("latent after encode shape", latent.shape)
            latent = tf.repeat(latent , batch_size , axis=0)
            #print("latent after batch_size shape", latent.shape)
            latent = self.add_noise(latent, input_img_noise_t, noise)
        return latent, alphas, alphas_prev

    def get_model_output(
        self,
        latent,
        t,
        context,
        unconditional_context,
        unconditional_guidance_scale,
        batch_size,
    ):
        timesteps = np.array([t])
        t_emb = self.timestep_embedding(timesteps)
        t_emb = np.repeat(t_emb, batch_size, axis=0)
        unconditional_latent = self.diffusion_model.predict_on_batch(
            [latent, t_emb, unconditional_context]
        )
        latent = self.diffusion_model.predict_on_batch([latent, t_emb, context])
        return unconditional_latent + unconditional_guidance_scale * (
            latent - unconditional_latent
        )

    def get_x_prev_and_pred_x0(self, x, e_t, index, a_t, a_prev):
        sqrt_one_minus_at = math.sqrt(1 - a_t)
        pred_x0 = (x - sqrt_one_minus_at * e_t) / math.sqrt(a_t)

        sigma_t = 0
        dir_xt = math.sqrt(1.0 - a_prev - sigma_t**2) * e_t # Direction pointing to x_t
        x_prev = math.sqrt(a_prev) * pred_x0 + dir_xt
        return x_prev, pred_x0

def get_models(img_height, img_width, download_weights=True):
    n_h = img_height // 8
    n_w = img_width // 8

    # Create text encoder
    input_word_ids = keras.layers.Input(shape=(MAX_TEXT_LEN,), dtype="int32")
    input_pos_ids = keras.layers.Input(shape=(MAX_TEXT_LEN,), dtype="int32")
    embeds = CLIPTextTransformer()([input_word_ids, input_pos_ids])
    text_encoder = keras.models.Model([input_word_ids, input_pos_ids], embeds)

    # Creation diffusion UNet
    context = keras.layers.Input((MAX_TEXT_LEN, 768))
    t_emb = keras.layers.Input((320,))
    latent = keras.layers.Input((n_h, n_w, 4))
    unet = UNetModel()
    diffusion_model = keras.models.Model(
        [latent, t_emb, context], unet([latent, t_emb, context])
    )

    # Create decoder
    latent = keras.layers.Input((n_h, n_w, 4))
    decoder = Decoder()
    decoder = keras.models.Model(latent, decoder(latent))

    inp_img = keras.layers.Input((img_height, img_width, 3))
    encoder = Encoder()
    encoder = keras.models.Model(inp_img, encoder(inp_img))

    if download_weights:
        text_encoder_weights_fpath = keras.utils.get_file(
            origin="https://huggingface.co/fchollet/stable-diffusion/resolve/main/text_encoder.h5",
            file_hash="d7805118aeb156fc1d39e38a9a082b05501e2af8c8fbdc1753c9cb85212d6619",
        )
        diffusion_model_weights_fpath = keras.utils.get_file(
            origin="https://huggingface.co/fchollet/stable-diffusion/resolve/main/diffusion_model.h5",
            file_hash="a5b2eea58365b18b40caee689a2e5d00f4c31dbcb4e1d58a9cf1071f55bbbd3a",
        )
        decoder_weights_fpath = keras.utils.get_file(
            origin="https://huggingface.co/fchollet/stable-diffusion/resolve/main/decoder.h5",
            file_hash="6d3c5ba91d5cc2b134da881aaa157b2d2adc648e5625560e3ed199561d0e39d5",
        )

        encoder_weights_fpath = keras.utils.get_file(
            origin="https://huggingface.co/divamgupta/stable-diffusion-tensorflow/resolve/main/encoder_newW.h5",
            file_hash="56a2578423c640746c5e90c0a789b9b11481f47497f817e65b44a1a5538af754",
        )

        text_encoder.load_weights(text_encoder_weights_fpath)
        diffusion_model.load_weights(diffusion_model_weights_fpath)
        decoder.load_weights(decoder_weights_fpath)
        encoder.load_weights(encoder_weights_fpath)
    return text_encoder, diffusion_model, decoder , encoder
