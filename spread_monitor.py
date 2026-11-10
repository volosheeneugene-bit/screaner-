#!/usr/bin/env python3
"""
Монитор спреда MNT/USDT: Bybit (спот) <-> Agni Finance (Mantle, Uniswap V3-форк).

Зависимости: только requests  ->  pip install requests

Как это работает:
  * CEX: публичный REST Bybit v5 /market/tickers (bid1Price / ask1Price).
  * DEX: JSON-RPC eth_call к пулу Agni (slot0 -> sqrtPriceX96), без web3.
    Порядок токенов (token0/token1) и decimals читаются из контракта
    автоматически, поэтому цена считается корректно в любом порядке.
  * DEX Bid/Ask = спотовая цена пула -/+ комиссия пула (fee tier).
    Net Spread = спред уже с учётом комиссии пула (без проскальзывания и газа).
  * Фильтр: в консоль и в CSV попадают только итерации, где Net Spread
    по любому направлению >= MIN_SPREAD_THRESHOLD.
  * Если спред ниже порога, скрипт работает тихо и раз в STATUS_INTERVAL
    секунд печатает короткую строку-статус, чтобы было видно, что он жив.
"""

import csv
import os
import sys
import time
from datetime import datetime
from decimal import Decimal, getcontext

import requests

getcontext().prec = 50

# ============================ НАСТРОЙКИ ============================
# !!! ВАЖНО: вставьте сюда адрес пула Agni MNT/USDT (Uniswap V3-style pool).
POOL_ADDRESS = "0xd08c50f7e69e9aeb2867deff4a8053d9a855e26a"


# Токены Mantle (для проверки, что указан нужный пул). Сверьте на Mantlescan.
WMNT_ADDRESS = "0x78c1b0C915c4FAA5FffA6CAbf0219DA4b6c0A3a8".lower()
USDT_ADDRESS = "0x201EBa5CC46D216Ce6DC03F6a759e8E766e956aE".lower()

MANTLE_RPC = "https://rpc.mantle.xyz"
BYBIT_URL = "https://api.bybit.com/v5/market/tickers"
BYBIT_SYMBOL = "MNTUSDT"

POLL_INTERVAL = 4         # пауза между итерациями, сек
HTTP_TIMEOUT = 5          # таймаут запросов, сек

MIN_SPREAD_THRESHOLD = Decimal("0.5")  # порог чистого спреда, % (вывод + запись в CSV)
STATUS_INTERVAL = 30                   # раз в N сек печатать статус «я жив», если спред ниже порога
CSV_FILE = "spread_log.csv"            # файл лога (создаётся автоматически)
CSV_COLUMNS = ["timestamp", "cex_bid", "cex_ask", "dex_price", "direction", "net_spread"]

# Селекторы функций (первые 4 байта keccak256 сигнатуры)
SEL_SLOT0 = "0x3850c7bd"     # slot0()
SEL_TOKEN0 = "0x0dfe1681"    # token0()
SEL_TOKEN1 = "0xd21220a7"    # token1()
SEL_FEE = "0xddca3f43"       # fee()
SEL_DECIMALS = "0x313ce567"  # decimals()
# ===================================================================

# ANSI-цвета
GREEN, RED, YELLOW, CYAN, GRAY, RESET = (
    "\033[92m", "\033[91m", "\033[93m", "\033[96m", "\033[90m", "\033[0m"
)

DIR_CEX_TO_DEX = "CEX->DEX"   # купить на CEX, продать на DEX
DIR_DEX_TO_CEX = "DEX->CEX"   # купить на DEX, продать на CEX

session = requests.Session()


# ----------------------------- RPC / DEX -----------------------------
def rpc_call(to: str, data: str) -> str:
    """Выполняет eth_call и возвращает hex-строку результата."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    r = session.post(MANTLE_RPC, json=payload, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    body = r.json()
    if "error" in body:
        raise RuntimeError(f"RPC error: {body['error']}")
    result = body.get("result")
    if not result or result == "0x":
        raise RuntimeError("Пустой ответ eth_call (неверный адрес контракта?)")
    return result


def word(hex_result: str, index: int) -> int:
    """Достаёт 32-байтное слово №index из ответа как int."""
    raw = hex_result[2:]
    return int(raw[index * 64:(index + 1) * 64], 16)


def addr_from_word(hex_result: str) -> str:
    return "0x" + hex_result[2:][24:64].lower()


class PoolInfo:
    """Статические данные пула — читаются один раз при старте."""

    def __init__(self, pool: str):
        self.pool = pool
        self.token0 = addr_from_word(rpc_call(pool, SEL_TOKEN0))
        self.token1 = addr_from_word(rpc_call(pool, SEL_TOKEN1))
        self.dec0 = word(rpc_call(self.token0, SEL_DECIMALS), 0)
        self.dec1 = word(rpc_call(self.token1, SEL_DECIMALS), 0)
        fee_raw = word(rpc_call(pool, SEL_FEE), 0)      # в 1e-6, напр. 2500 = 0.25%
        self.fee = Decimal(fee_raw) / Decimal(1_000_000)

        pair = {self.token0, self.token1}
        if pair != {WMNT_ADDRESS, USDT_ADDRESS}:
            print(f"{YELLOW}[WARN] Токены пула ({self.token0}, {self.token1}) не совпадают "
                  f"с WMNT/USDT из констант. Проверьте POOL_ADDRESS!{RESET}")

        # MNT является token0? Нужно для направления пересчёта цены.
        self.mnt_is_token0 = self.token0 == WMNT_ADDRESS


def get_dex_price(info: PoolInfo) -> Decimal:
    """Цена 1 MNT в USDT из sqrtPriceX96 пула."""
    sqrt_price_x96 = word(rpc_call(info.pool, SEL_SLOT0), 0)
    if sqrt_price_x96 == 0:
        raise RuntimeError("sqrtPriceX96 = 0 (пул не инициализирован)")

    # raw = token1 / token0 в «сырых» единицах = (sqrtP / 2^96)^2
    ratio = (Decimal(sqrt_price_x96) / Decimal(2 ** 96)) ** 2
    # Корректировка на decimals -> token1 за 1 token0 в человеческих единицах
    price_t1_per_t0 = ratio * Decimal(10) ** (info.dec0 - info.dec1)

    # Если MNT = token0, цена MNT в USDT = t1/t0, иначе инвертируем
    return price_t1_per_t0 if info.mnt_is_token0 else Decimal(1) / price_t1_per_t0


# ------------------------------- CEX ---------------------------------
def get_cex_quote() -> tuple[Decimal, Decimal]:
    """Best Bid и Best Ask спота Bybit."""
    r = session.get(
        BYBIT_URL,
        params={"category": "spot", "symbol": BYBIT_SYMBOL},
        timeout=HTTP_TIMEOUT,
    )
    r.raise_for_status()
    body = r.json()
    if body.get("retCode") != 0:
        raise RuntimeError(f"Bybit error: {body.get('retMsg')}")
    item = body["result"]["list"][0]
    bid, ask = Decimal(item["bid1Price"]), Decimal(item["ask1Price"])
    if bid <= 0 or ask <= 0:
        raise RuntimeError("Bybit вернул нулевой bid/ask")
    return bid, ask


# ------------------------------- CSV ---------------------------------
def append_csv_rows(rows: list[list[str]]) -> None:
    """Дописывает строки в CSV. Если файла нет (или он пуст) — создаёт с шапкой."""
    need_header = not os.path.exists(CSV_FILE) or os.path.getsize(CSV_FILE) == 0
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if need_header:
            writer.writerow(CSV_COLUMNS)
        writer.writerows(rows)


# ------------------------------ Вывод --------------------------------
def fmt_spread(value: Decimal) -> str:
    color = GREEN if value >= MIN_SPREAD_THRESHOLD else GRAY
    return f"{color}{value:+.4f}%{RESET}"


def main() -> None:
    if int(POOL_ADDRESS, 16) == 0:
        print(f"{RED}Укажите адрес пула Agni MNT/USDT в константе POOL_ADDRESS.{RESET}")
        sys.exit(1)

    # Инициализация пула с повторами — скрипт не должен падать
    info = None
    while info is None:
        try:
            info = PoolInfo(POOL_ADDRESS)
        except Exception as e:  # noqa: BLE001
            print(f"{YELLOW}[INIT] Не удалось прочитать пул: {e}. Повтор через 5 с...{RESET}")
            time.sleep(5)

    print(f"{CYAN}Пул: {info.pool}\n"
          f"token0={info.token0} (dec {info.dec0}) | token1={info.token1} (dec {info.dec1})\n"
          f"Комиссия пула: {info.fee * 100:.4f}% | MNT = token{'0' if info.mnt_is_token0 else '1'}\n"
          f"Порог Net Spread: {MIN_SPREAD_THRESHOLD}% | лог: {CSV_FILE}{RESET}\n")

    last_status = time.monotonic()
    best_seen = None  # лучший Net Spread с момента последнего статуса

    while True:
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        try:
            cex_bid, cex_ask = get_cex_quote()
            dex_mid = get_dex_price(info)

            # Оценка DEX Bid/Ask: спот ± комиссия пула (без проскальзывания)
            dex_ask = dex_mid * (Decimal(1) + info.fee)   # цена покупки MNT на DEX
            dex_bid = dex_mid * (Decimal(1) - info.fee)   # цена продажи MNT на DEX

            # Net Spread (с учётом комиссии пула)
            net1 = (dex_bid - cex_ask) / cex_ask * 100    # купить на CEX, продать на DEX
            net2 = (cex_bid - dex_ask) / dex_ask * 100    # купить на DEX, продать на CEX

            best = max(net1, net2)
            best_seen = best if best_seen is None else max(best_seen, best)

            # Направления, превысившие порог
            hits = [(d, s) for d, s in ((DIR_CEX_TO_DEX, net1), (DIR_DEX_TO_CEX, net2))
                    if s >= MIN_SPREAD_THRESHOLD]

            if hits:
                # 1) Консоль
                print(
                    f"{GRAY}[{ts}]{RESET} "
                    f"CEX bid/ask: {cex_bid:.5f}/{cex_ask:.5f} | "
                    f"DEX mid: {dex_mid:.5f} (bid {dex_bid:.5f} / ask {dex_ask:.5f})"
                )
                print(
                    f"           {DIR_CEX_TO_DEX}: {fmt_spread(net1)} | "
                    f"{DIR_DEX_TO_CEX}: {fmt_spread(net2)}\n"
                )

                # 2) CSV (отдельная защита: сбой записи не должен ронять скрипт)
                try:
                    append_csv_rows([
                        [
                            now.isoformat(timespec="seconds"),
                            f"{cex_bid:.8f}",
                            f"{cex_ask:.8f}",
                            f"{dex_mid:.8f}",
                            direction,
                            f"{spread:.4f}",
                        ]
                        for direction, spread in hits
                    ])
                except OSError as e:
                    print(f"{YELLOW}[CSV] Не удалось записать в {CSV_FILE}: {e}{RESET}")

                last_status = time.monotonic()
                best_seen = None
            elif time.monotonic() - last_status >= STATUS_INTERVAL:
                # Тихий режим: короткий статус «скрипт жив»
                print(f"{GRAY}[{ts}] . жив | лучший Net Spread за период: "
                      f"{best_seen:+.4f}% (порог {MIN_SPREAD_THRESHOLD}%){RESET}")
                last_status = time.monotonic()
                best_seen = None

        except requests.exceptions.Timeout:
            print(f"{GRAY}[{ts}]{RESET} {YELLOW}Таймаут сети/RPC, пропускаю итерацию{RESET}")
        except requests.exceptions.RequestException as e:
            print(f"{GRAY}[{ts}]{RESET} {YELLOW}Сетевая ошибка: {e}{RESET}")
        except (RuntimeError, KeyError, IndexError, ValueError, ArithmeticError) as e:
            print(f"{GRAY}[{ts}]{RESET} {YELLOW}Ошибка данных: {e}{RESET}")
        except Exception as e:  # noqa: BLE001 — страховка, чтобы скрипт не падал
            print(f"{GRAY}[{ts}]{RESET} {RED}Неожиданная ошибка: {e!r}{RESET}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nОстановлено пользователем.")
