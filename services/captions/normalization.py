import re


DEVANAGARI_ROMAN_WORDS = {
    "\u0924\u0941\u092e": "tum",
    "\u0915\u0948\u0938\u0947": "kaise",
    "\u0915\u0947\u0938\u0947": "kaise",
    "\u0939\u094b": "ho",
    "\u0939\u0948": "hai",
    "\u0939\u0948\u0902": "hain",
    "\u092e\u0948\u0902": "main",
    "\u092e\u0947": "me",
    "\u092e\u0941\u091d\u0947": "mujhe",
    "\u0915\u094d\u092f\u093e": "kya",
    "\u0915\u094d\u092f\u094b\u0902": "kyun",
    "\u0928\u0939\u0940\u0902": "nahi",
    "\u0939\u093e\u0901": "haan",
    "\u092f\u0939": "yeh",
}

URDU_ROMAN_WORDS = {
    "\u062a\u0645": "tum",
    "\u06a9\u06cc\u0633\u06d2": "kaise",
    "\u06c1\u0648": "ho",
    "\u06c1\u06d2": "hai",
    "\u06c1\u06cc\u06ba": "hain",
    "\u0645\u06cc\u06ba": "main",
    "\u0645\u062c\u06be\u06d2": "mujhe",
    "\u06a9\u06cc\u0627": "kya",
    "\u06a9\u06cc\u0648\u06ba": "kyun",
    "\u0646\u06c1\u06cc\u06ba": "nahi",
    "\u06c1\u0627\u06ba": "haan",
}

DEVANAGARI_ROMAN_CHARS = {
    "\u0905": "a", "\u0906": "aa", "\u0907": "i", "\u0908": "ee",
    "\u0909": "u", "\u090a": "oo", "\u090f": "e", "\u0910": "ai",
    "\u0913": "o", "\u0914": "au", "\u0915": "k", "\u0916": "kh",
    "\u0917": "g", "\u0918": "gh", "\u091a": "ch", "\u091b": "chh",
    "\u091c": "j", "\u091d": "jh", "\u091f": "t", "\u0920": "th",
    "\u0921": "d", "\u0922": "dh", "\u0924": "t", "\u0925": "th",
    "\u0926": "d", "\u0927": "dh", "\u0928": "n", "\u092a": "p",
    "\u092b": "f", "\u092c": "b", "\u092d": "bh", "\u092e": "m",
    "\u092f": "y", "\u0930": "r", "\u0932": "l", "\u0935": "v",
    "\u0936": "sh", "\u0937": "sh", "\u0938": "s", "\u0939": "h",
    "\u093e": "a", "\u093f": "i", "\u0940": "ee", "\u0941": "u",
    "\u0942": "oo", "\u0947": "e", "\u0948": "ai", "\u094b": "o",
    "\u094c": "au", "\u0902": "n", "\u0901": "n", "\u093c": "",
    "\u094d": "", "\u0964": ".", "\u0965": ".",
}

URDU_ROMAN_CHARS = {
    "\u0627": "a", "\u0622": "aa", "\u0628": "b", "\u067e": "p",
    "\u062a": "t", "\u0679": "t", "\u062b": "s", "\u062c": "j",
    "\u0686": "ch", "\u062d": "h", "\u062e": "kh", "\u062f": "d",
    "\u0688": "d", "\u0630": "z", "\u0631": "r", "\u0691": "r",
    "\u0632": "z", "\u0698": "zh", "\u0633": "s", "\u0634": "sh",
    "\u0635": "s", "\u0636": "z", "\u0637": "t", "\u0638": "z",
    "\u0639": "a", "\u063a": "gh", "\u0641": "f", "\u0642": "q",
    "\u06a9": "k", "\u06af": "g", "\u0644": "l", "\u0645": "m",
    "\u0646": "n", "\u06ba": "n", "\u0648": "o", "\u06c1": "h",
    "\u06be": "h", "\u0621": "", "\u06cc": "i", "\u06d2": "e",
    "\u064e": "", "\u064f": "", "\u0650": "",
}


def romanize_caption_text(text: str) -> str:
    romanized = str(text or "")
    for source, replacement in {**DEVANAGARI_ROMAN_WORDS, **URDU_ROMAN_WORDS}.items():
        romanized = romanized.replace(source, replacement)

    output = []
    for char in romanized:
        if "\u0900" <= char <= "\u097f":
            output.append(DEVANAGARI_ROMAN_CHARS.get(char, " "))
        elif "\u0600" <= char <= "\u06ff":
            output.append(URDU_ROMAN_CHARS.get(char, " "))
        else:
            output.append(char)
    return re.sub(r"\s+", " ", "".join(output)).strip()


def normalize_caption_text(text: str, caption_mode: str = "hinglish") -> str:
    cleaned = " ".join(str(text or "").split())
    if str(caption_mode or "").lower() == "original":
        return cleaned
    return romanize_caption_text(cleaned)

