import csv
import io
import time
from datetime import date, timedelta

import requests


MOEX_TIMEOUT = 35
MAX_RETRIES = 4
PAGE_SIZE = 500


def chunks(start_date, end_date):
    """
    Делит большой период на части примерно по 31 дню.
    Это снижает риск таймаутов MOEX ISS.
    """
    while start_date <= end_date:
        chunk_end = min(
            start_date + timedelta(days=30),
            end_date,
        )

        yield start_date, chunk_end

        start_date = chunk_end + timedelta(days=1)


def _request_json(url, params):
    """
    Запрос к MOEX ISS с повторными попытками.
    """
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(
                url,
                params=params,
                timeout=MOEX_TIMEOUT,
                headers={
                    "User-Agent": "AA-Analitik-MOEX-Downloader/1.0"
                },
            )

            response.raise_for_status()
            return response.json()

        except Exception as exc:
            last_error = exc

            if attempt < MAX_RETRIES - 1:
                time.sleep(1 + attempt)

    raise RuntimeError(
        f"MOEX ISS request failed: {last_error}"
    )


def _get_url(secid, market):
    """
    Возвращает правильный endpoint MOEX ISS.
    """
    secid = secid.strip().upper()

    if market == "stock":
        return (
            "https://iss.moex.com/iss/engines/stock/"
            f"markets/shares/securities/{secid}/candles.json"
        )

    if market == "futures":
        return (
            "https://iss.moex.com/iss/engines/futures/"
            f"markets/forts/securities/{secid}/candles.json"
        )

    raise ValueError(
        "market должен быть 'stock' или 'futures'"
    )


def fetch(secid, market, start, end):
    """
    Загружает минутные свечи MOEX за указанный период.

    Возвращает CSV в bytes.
    """

    secid = secid.strip().upper()

    if not secid:
        raise ValueError("Пустой SECID")

    if not isinstance(start, date):
        raise TypeError("start должен быть date")

    if not isinstance(end, date):
        raise TypeError("end должен быть date")

    if start > end:
        raise ValueError(
            "Дата начала не может быть позже даты окончания"
        )

    url = _get_url(secid, market)

    # Используем словарь для удаления возможных дублей.
    rows = {}

    for chunk_start, chunk_end in chunks(start, end):

        cursor = 0

        while True:

            params = {
                "interval": 1,
                "from": chunk_start.isoformat(),
                "till": chunk_end.isoformat(),
                "start": cursor,
            }

            payload = _request_json(
                url,
                params,
            )

            candles = payload.get(
                "candles",
                {},
            )

            columns = candles.get(
                "columns",
                [],
            )

            data = candles.get(
                "data",
                [],
            )

            if not data:
                break

            for raw_row in data:

                row = dict(
                    zip(columns, raw_row)
                )

                key = (
                    row.get("begin"),
                    row.get("open"),
                    row.get("high"),
                    row.get("low"),
                    row.get("close"),
                    row.get("volume"),
                )

                rows[key] = row

            # Если пришло меньше страницы,
            # значит дальше данных нет.
            if len(data) < PAGE_SIZE:
                break

            cursor += len(data)

    output_columns = [
        "begin",
        "end",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    ]

    output = io.StringIO(
        newline=""
    )

    writer = csv.DictWriter(
        output,
        fieldnames=output_columns,
    )

    writer.writeheader()

    sorted_rows = sorted(
        rows.values(),
        key=lambda item: str(
            item.get("begin", "")
        ),
    )

    for row in sorted_rows:

        writer.writerow(
            {
                column: row.get(
                    column,
                    "",
                )
                for column in output_columns
            }
        )

    return output.getvalue().encode(
        "utf-8"
    )


def fetch_many(
    secids,
    market,
    start,
    end,
):
    """
    Удобная функция для загрузки
    нескольких инструментов.
    """

    result = {}

    for secid in secids:

        secid = secid.strip().upper()

        if not secid:
            continue

        result[secid] = fetch(
            secid,
            market,
            start,
            end,
        )

    return result
