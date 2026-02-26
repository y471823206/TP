import sys

from transformers import AutoTokenizer, AutoModelForCausalLM


MODEL_NAME = "tencent/HY-MT1.5-1.8B"
_tokenizer = None
_model = None


def _load_model():
    global _tokenizer, _model
    if _tokenizer is not None and _model is not None:
        return
    print("Loading Hunyuan HY-MT1.5-1.8B model...", file=sys.stderr)
    _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME, trust_remote_code=True)
    _model = AutoModelForCausalLM.from_pretrained(
        MODEL_NAME,
        trust_remote_code=True,
        device_map="auto",
    )


def translate_to_zh(source_lang: str, text: str) -> str:
    # 目前场景统一翻译成中文，和 validator.py 里的 mt_zh 字段保持一致
    prompt = (
        "将以下文本翻译为中文，注意只需要输出翻译后的结果，不要额外解释：\n\n"
        f"{text}"
    )
    inputs = _tokenizer(prompt, return_tensors="pt").to(_model.device)
    input_length = inputs["input_ids"].shape[1]
    outputs = _model.generate(**inputs, max_new_tokens=256, do_sample=False)
    # 只解码新生成的 token，不包含 prompt，stdout 才是纯译文
    new_tokens = outputs[0][input_length:]
    raw = _tokenizer.decode(new_tokens, skip_special_tokens=True).strip()
    # 取第一段作为译文，避免模型续写多段导致 validator 拿到多余内容
    first_para = raw.split("\n\n")[0].strip() if raw else ""
    return first_para or raw


def main(argv=None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if len(argv) < 2:
        print("Usage: hy_mt_cli.py <source_lang> <text>", file=sys.stderr)
        return 1

    source_lang = argv[0]
    text = " ".join(argv[1:])

    _load_model()
    translation = translate_to_zh(source_lang, text)
    # 只输出译文文本，方便 validator.py 直接取 stdout 当 mt_zh
    print(translation)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

