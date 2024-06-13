import os
os.environ["CUDA_VISIBLE_DEVICES"] = "1"
from typing import Optional
import pandas as pd
import re
import json
import warnings
import datasets
import torch
from datasets import load_dataset, Dataset
from peft import LoraConfig, AutoPeftModelForCausalLM, prepare_model_for_kbit_training, get_peft_model
from transformers import (AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig, AutoTokenizer, TrainingArguments, GenerationConfig)
import tqdm
from tqdm import tqdm
import tensorrt as trt
from trl import SFTTrainer

os.environ["WANDB_DISABLED"] = "true"
warnings.filterwarnings("ignore")

base_folder = "E:/HuggingFace/models/TII/"

# 1. Load the Dataset
dataset = load_dataset("bigbio/med_qa")
train_data = dataset['train']
val_data = dataset['validation']
test_data = dataset['test']

# 2. Preset the the Instruction-based prompt template
def generate_prompt(x):
    answer_idx = 'Nothing'
    if x['answer_idx'] == 'A':
        answer_idx = x['opa']
    elif x['answer_idx'] == 'B':
        answer_idx = x['opb']
    elif x['answer_idx'] == 'C':
        answer_idx = x['opc']
    elif x['answer_idx'] == 'D':
        answer_idx = x['opd']
    elif x['answer_idx'] == 'E':
        answer_idx = x['ope']
    question = '{}\nOptions:\n1. {}\n2. {}\n3. {}\n4. {}\n5. {}\n'.format(x['question'], 
                                                                          x['opa'],
                                                                          x['opb'], 
                                                                          x['opc'], 
                                                                          x['opd'], 
                                                                          x['ope'])
    answer = answer_idx
    prompt = f"""Question:
    {question}
    [INST] Solve this medical question-answering and provide the correct option. [/INST]
    Answer: {answer} </s>""" 
    return prompt

def generate_and_tokenize_prompt(prompt):
    return tokenizer(generate_prompt(prompt), padding = "max_length", truncation = True, max_length = 2048)

def convert_format_df(data):
    data_extracted = [
    {'question' : variable['question'], 
     'answer_idx' : variable['answer_idx'], 
     'answer' : variable['answer'],
     'opa' : variable['options'][0]['value'], 
     'opb' : variable['options'][1]['value'], 
     'opc' : variable['options'][2]['value'], 
     'opd' : variable['options'][3]['value'], 
     'ope' : variable['options'][4]['value']}
    for variable in data
    ]
    df = pd.DataFrame(data_extracted)
    df['text'] = df.apply(lambda x: generate_prompt(x), axis = 1)
    data_hf = Dataset.from_pandas(df)
    return df, data_hf

train_df, train_hf = convert_format_df(train_data)
val_df, val_hf = convert_format_df(val_data)
test_df, test_hf = convert_format_df(test_data)

# 3. Set the quantization settings
bnb_config = BitsAndBytesConfig(
    load_in_4bit = True,
    bnb_4bit_quant_type = "nf4",
    bnb_4bit_compute_dtype = torch.bfloat16,
    bnb_4bit_use_double_quant = False,
)

# 4. Select the MistralAI's Mistral-7B-Instruct model
model = AutoModelForCausalLM.from_pretrained(
    base_folder + "Falcon-7B-Instruct",
    quantization_config = bnb_config,
    #attn_implementation = "flash_attention_2",
    torch_dtype = torch.bfloat16,
    device_map = "auto",
    use_auth_token = False,
    trust_remote_code = True,
)
model.config.use_cache = False
model.config.pretraining_tp = 1
model.gradient_checkpointing_enable()
model = prepare_model_for_kbit_training(model)
peft_config = LoraConfig(
    lora_alpha = 32,
    lora_dropout = 0.05,
    r = 16,
    bias = "none",
    task_type = "CAUSAL_LM",
    target_modules = ["query_key_value", "dense", "dense_h_to_4h", "dense_4h_to_h"]
)
model = get_peft_model(model, peft_config)

# 4.1 Select the tokenizer
tokenizer = AutoTokenizer.from_pretrained(base_folder + "Falcon-7B-Instruct", 
                                          padding = "max_length", 
                                          truncation = True, 
                                          max_length = 2048)
tokenizer.pad_token = tokenizer.unk_token
tokenizer.pad_token_id = tokenizer.unk_token_id
tokenizer.padding_side = 'left'

training_arguments = TrainingArguments(
    output_dir = "./Results/MedQA/Falcon-7b-Instruct",
    num_train_epochs = 4,
    per_device_train_batch_size = 8,
    gradient_accumulation_steps = 1,
    optim = "paged_adamw_8bit",
    save_strategy = "epoch",
    logging_steps = 100,
    logging_strategy = "steps",
    learning_rate = 2e-4,
    bf16 = False,
    fp16 = False, 
    max_grad_norm = 0.3,
    lr_scheduler_type = "constant",
    group_by_length = True,
    disable_tqdm = False,
    report_to = None
)

# 5. Training the model
trainer = SFTTrainer(
    model = model,
    train_dataset = train_hf,
    eval_dataset = val_hf,
    peft_config = peft_config,
    dataset_text_field = "text",
    max_seq_length = 2048,
    tokenizer = tokenizer,
    args = training_arguments,
    packing = False,
)

trainer.train()

# 6. Test and compare the non-fine-tuned model against the fine-tuned MistralAI's model
import tqdm

# Load the best checkpoint of Mistral-7B-Instruct
model_id = 'Results\MedQA\Falcon-7b-Instruct\checkpoint-5092'
tokenizer = AutoTokenizer.from_pretrained(model_id)
tokenizer.pad_token = tokenizer.unk_token
tokenizer.pad_token_id =  tokenizer.unk_token_id
tokenizer.padding_side = 'left'
model = AutoPeftModelForCausalLM.from_pretrained(
    model_id,
    low_cpu_mem_usage = True,
    return_dict = True,
    torch_dtype = torch.bfloat16,
    device_map = "cuda")

generation_config = GenerationConfig(
    do_sample = True,
    top_k = 1,
    temperature = 0.1,
    max_new_tokens = 25,
    pad_token_id = tokenizer.pad_token_id
)

# 7. Load the Test set
def solve_question(question_prompt):
    inputs = tokenizer(question_prompt, return_tensors = "pt", padding = True, truncation = True).to("cuda")
    outputs = model.generate(**inputs, generation_config = generation_config)
    answer = tokenizer.batch_decode(outputs, skip_special_tokens = True)
    return answer

def remove_words_after_inst(text):
    # Regex pattern to match "[/INST]\n" followed by any characters until the end of the sentence
    pattern = r'Answer:.*'
    # Using re.sub to replace the matched part with an empty string
    result = re.sub(pattern, 'Answer: ', text, flags=re.DOTALL)
    return result

all_answers = []
test_prompts = list(test_df['text'])
for i in tqdm.tqdm(range(0, len(test_prompts), 16)):
    question_prompts = test_prompts[i:i+16]
    question_prompts2 = [remove_words_after_inst(text) for text in question_prompts]
    ans = solve_question(question_prompts2)
    ans_option = []
    for text in ans:
        ans_option.append(re.search(r'Answer: \s*(.*)', text).group(1))
    all_answers.extend(ans_option)

all_answers_1 = [re.sub(r'</s>|://|</s|</|s>|s/|.swing', '', answers) for answers in all_answers]
url_pattern = r'\b\S*\.com\S*|\b\S*\.gov\S*|\b\S*\.org\S*|\b\S*\.jpg'
all_answers_2 = [re.sub(url_pattern, '', answers) for answers in all_answers_1]
all_answers_3 = [re.sub(r'\bThe\b[^.!?]*[.!?]?|\bWhich\b[^.!?]*[.!?]?|\bWhat\b[^.!?]*[.!?]?|\(Options.*', '', answers) for answers in all_answers_2]
all_answers_4 = [answers.strip() for answers in all_answers_3]
all_answers_4


# 8. Score for the accuracy on Test set
correct_answers = []
for i in range(len(test_df)):
    if test_df['answer_idx'][i] == 'A':
        correct_answers.append(test_df['opa'][i])
    elif test_df['answer_idx'][i] == 'B':
        correct_answers.append(test_df['opb'][i])
    elif test_df['answer_idx'][i] == 'C':
        correct_answers.append(test_df['opc'][i])
    elif test_df['answer_idx'][i] == 'D':
        correct_answers.append(test_df['opd'][i])
    elif test_df['answer_idx'][i] == 'E':
        correct_answers.append(test_df['ope'][i])


correct_answer = [string.lower() for string in correct_answers]
all_answer = [string.lower() for string in all_answers]


def filter_related_words(predicted_answer, correct_answer):
    # Tokenize the correct answer into words
    correct_words = correct_answer.split()
    
    # Create a regex pattern to match any of the correct words
    pattern = r'\b(?:' + '|'.join(map(re.escape, correct_words)) + r')\b'
    
    # Find all matching words in the predicted answer
    matches = re.findall(pattern, predicted_answer)
    
    # Join the matches into a string
    result = ' '.join(matches)
    
    return result

def match_answers(predicted_answers, correct_answers):
    filtered_answers = []
    for pred, corr in zip(predicted_answers, correct_answers):
        filtered_answers.append(filter_related_words(pred, corr))
    return filtered_answers

matched_answers = match_answers(all_answer, correct_answer)
for pred, matched, corr in zip(all_answer, matched_answers, correct_answer):
    print(f"Predicted: {pred}")
    print(f"Matched: {matched}")
    print(f"Correct: {corr}")
    print("---")


correct_count = 0
for i in range(len(test_df)):
    correct_count += correct_answer[i] == matched_answers[i]
correct_count/len(test_df)
