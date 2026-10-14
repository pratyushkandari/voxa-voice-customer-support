"""
Voxa V1 Exploratory Prototype: Flask Seq2Seq Inference Server.

Historical context:
This module represents the initial V1 monolithic prototype for evaluating 
FLAN-T5 Seq2Seq text generation prior to the V2 distributed streaming architecture.
"""

import os
import logging
from flask import Flask, request, jsonify
from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
import torch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("voxa.v1.backend")

app = Flask(__name__)

# Model Checkpoint Resolution
LOCAL_CHECKPOINT = "model3"
DEFAULT_MODEL = "google/flan-t5-base"

model_path = LOCAL_CHECKPOINT if os.path.exists(LOCAL_CHECKPOINT) else DEFAULT_MODEL
logger.info(f"Initializing Seq2Seq model from: {model_path}")

try:
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForSeq2SeqLM.from_pretrained(model_path)
    model.eval()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    logger.info(f"Inference engine ready on compute device: {device}")
except Exception as exc:
    logger.warning(f"Could not initialize model ({exc}). Activating deterministic fallback.")
    tokenizer = None
    model = None
    device = torch.device("cpu")


@app.route("/chat", methods=["POST"])
def chat():
    """
    Synchronous inference endpoint for exploratory chat evaluations.
    Applies bounded nucleus sampling with repetition penalties to constrain p99 tail latency.
    """
    payload = request.get_json(silent=True) or {}
    user_input = payload.get("message", "").strip()

    if not user_input:
        return jsonify({"error": "Empty message payload"}), 400

    prompt = f"You are a helpful and friendly customer support assistant. Answer the customer query: {user_input}"

    # Deterministic fallback when checkpoint is not loaded in host environment
    if not model or not tokenizer:
        return jsonify({
            "response": f"Voxa Support (Echo Mode): Thank you for your inquiry regarding '{user_input}'. "
                        f"Standard return window is 30 days and orders deliver in 3-5 days."
        })

    inputs = tokenizer(prompt, return_tensors="pt").to(device)
    
    with torch.no_grad():
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

    response = tokenizer.decode(outputs[0], skip_special_tokens=True).strip()
    return jsonify({"response": response})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting V1 prototype Flask server on port {port}...")
    app.run(host="0.0.0.0", port=port, threaded=True)