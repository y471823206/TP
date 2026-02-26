import sys
from typing import Optional

import torch
from fastapi import FastAPI
from pydantic import BaseModel
from transformers import AutoTokenizer, AutoModelForCausalLM


MODEL_NAME = "tencent/HY-MT1.5-1.8B"

print("Loading Hunyuan HY-MT1.5-1.8B model...", file=sys.stderr)
torch.set_num_threads(16)
tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    MODEL_NAME,
    trust_remote_code=True,
    device_map="cpu",
    torch_dtype=torch.float32,
)
model.eval()

app = FastAPI(title="Hunyuan HY-MT Local Server")


class MTRequest(BaseModel):
    source_lang: str = "auto"
    target_lang: str = "zh"
    text: str


def translate_to_zh(req: MTRequest) -> str:
    prompt = (
        "将以下文本翻译为中文，注意只需要输出翻译后的结果，不要额外解释：\n\n"
        f"{req.text}"
    )
    messages = [{"role": "user", "content": prompt}]
    formatted = tokenizer.apply_chat_template(
        messages,
        tokenize=False,
        add_generation_prompt=True,
    )
    input_ids = tokenizer(formatted, return_tensors="pt").input_ids.to("cpu")
    input_length = input_ids.shape[1]
    with torch.no_grad():
        outputs = model.generate(
            input_ids,
            max_new_tokens=32,
            do_sample=True,
            temperature=0.7,
            top_k=20,
            top_p=0.6,
            repetition_penalty=1.05,
        )
    new_tokens = outputs[0][input_length:]
    raw = tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    first_para = raw.split("\n\n")[0].strip() if raw else ""
    return first_para or raw


@app.post("/translate")
def translate(req: MTRequest):
    """
    请求/响应格式与 validator.py 的 call_hunyuan_mt_api 对齐：
    payload: {\"source_lang\", \"target_lang\", \"text\"}
    response: {\"translation\": \"...\"}
    """
    if req.target_lang != "zh":
        # 当前只实现翻译为中文，后续需要再扩展
        raise ValueError("当前服务仅支持 target_lang=zh")

    translation = translate_to_zh(req)
    return {"translation": translation}


if __name__ == "__main__":
    # 默认跑在 127.0.0.1:8000
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000, reload=False)

