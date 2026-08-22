# ============================
# BACKEND: Flask API (backend.py)
# ============================

from flask import Flask, request, jsonify
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import torch

app = Flask(__name__)

# ----------------------------
# Load Model and Tokenizer
# ----------------------------
# Specify the path or model ID from where to load the tokenizer and model.
# Here "model3" should be replaced with the actual folder or model identifier.
model_path = "model3"
print("Loading tokenizer and model from:", model_path)

# Load tokenizer - this converts text into tokens (numbers) the model understands.
tokenizer = AutoTokenizer.from_pretrained(model_path)

# Load the actual language model, designed for text-to-text generation tasks.
model = AutoModelForSeq2SeqLM.from_pretrained(model_path)

# Put the model in evaluation mode - disables training-specific layers like dropout.
model.eval()

# Use GPU if available for faster inference, otherwise fallback to CPU.
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model.to(device)
print(f"Model loaded on device: {device}")

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