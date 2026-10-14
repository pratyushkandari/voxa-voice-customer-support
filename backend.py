# ============================
# BACKEND: Flask API (backend.py)
# ============================

import os
from flask import Flask, request, jsonify
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import torch

app = Flask(__name__)

# ----------------------------
# Load Model and Tokenizer
# ----------------------------
# Check if fine-tuned local weights exist, else fallback to base FLAN-T5
local_dir = "model3"
if os.path.exists(local_dir):
    model_path = local_dir
    print(f"Loading custom fine-tuned model from: {model_path}")
else:
    model_path = "google/flan-t5-base"
    print(f"Local checkpoint '{local_dir}' not found. Falling back to public model: {model_path}")

try:
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_path)
    model.eval()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    print(f"Model loaded successfully on device: {device}")
except Exception as e:
    print(f"Warning: Could not pre-load model ({e}). Server will provide fallback responses.")
    tokenizer = None
    model = None
    device = torch.device("cpu")

# ----------------------------
# Define API Endpoint "/chat"
# ----------------------------
@app.route("/chat", methods=["POST"])
def chat():
    print("Received /chat request")

    # Extract user message from the POST request's JSON payload
    user_input = request.json.get("message", "")
    print(f"User input received: {user_input}")

    # Add context to the user input to guide the model for helpful & friendly responses
    prompt = f"You are a helpful and friendly customer support assistant. Answer the customer query: {user_input}"

    # Check if model is loaded
    if not model or not tokenizer:
        return jsonify({
            "response": f"Voxa Support (Echo Mode): Thank you for your inquiry regarding '{user_input}'. "
                        f"Standard return window is 30 days and orders deliver in 3-5 days."
        })

    # Tokenize the prompt and move tokens to the appropriate device (CPU/GPU)
    inputs = tokenizer(prompt, return_tensors="pt").to(device)
    print("Input tokenized and moved to device")

    # Generate a response using the model
    # - max_new_tokens limits output length to keep replies concise
    # - do_sample=True enables randomness for more natural and varied replies
    # - temperature controls creativity (0.7 is moderate creativity)
    # - top_k and top_p apply nucleus sampling to limit token choices, improving relevance
    # - repetition_penalty discourages repeated phrases to avoid boring output
    # - num_return_sequences=1 means generate a single response
    outputs = model.generate(
        inputs.input_ids,
        max_new_tokens=128,
        do_sample=True,
        temperature=0.7,
        top_k=50,
        top_p=0.9,
        repetition_penalty=1.2,
        num_return_sequences=1
    )
    print("Model generation complete")

    # Decode the output tokens back to human-readable text, skipping special tokens
    response = tokenizer.decode(outputs[0], skip_special_tokens=True)
    print(f"Response generated: {response}")

    # Return the generated response as a JSON object
    return jsonify({"response": response})

# ----------------------------
# Run Flask server on port 5000
# ----------------------------
if __name__== "__main__":
    print("Starting Flask server on port 5000...")
    # threaded=True allows handling multiple requests concurrently
    app.run(port=5000, threaded=True)