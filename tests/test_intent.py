from pprint import pprint

from services.intent_service import detect_intent


def main():
    text = (
        "Так, через 3 дня вроде это 16 число, да? Проверь. "
        "У меня будет занятие с детками в Баре. "
        "Само занятие думаю час, а вот дорога — 2 часа. "
        "Запиши и начало встречи, и учти дорогу, "
        "чтобы я знала, во сколько выехать."
    )

    result = detect_intent(text)

    pprint(result)


if __name__ == "__main__":
    main()