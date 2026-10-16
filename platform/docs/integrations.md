# Интеграции: какво е проверено и откъде

Всички факти са от официалните страници, прочетени на **09.10.2026**. Реални заявки към борсите,
TypeSafe и новинарските сайтове **не са правени** от средата за разработка (мрежата ги блокира).
Кодът е тестван с примерите от документацията и с тестови двойници, които са маркирани като такива.

## Bybit (основна борса)

| Тема | Какво е проверено | Източник |
|---|---|---|
| Регион | ЕИП (Bybit EU): REST `https://api.bybit.eu`; глобален: `https://api.bybit.com`. Изборът е `BYBIT_REGION=eu\|global`. | [V5 Integration Guidance](https://bybit-exchange.github.io/docs/v5/guide) |
| Лични ключове в Bybit EU | Обща документация: „EU site API only support Connect to Third-Party Applications for API broker user“. Обявата на Bybit EU от **02.09.2026** въвежда „API Transaction“ ключове с права и IP ограничения. Ако Bybit EU не ти позволи личен ключ, адаптерът ще получи грешка при проверка на ключа и таблото ще я покаже. | [Bybit EU обява](https://announcements.bybit.eu/en-EU/article/api-trading-power-your-tools-and-ai-with-deep-liquidity-and-low-latency--art639fafc104c9/) |
| Публичен поток | `wss://stream.bybit.com/v5/public/spot`. Отделен поток за ЕИП не е посочен, затова цените се показват като „Bybit публичен spot поток“, а не като цена на изпълнение в Bybit EU. Ping `{"op":"ping"}` на всеки 20 с. | [Connect](https://bybit-exchange.github.io/docs/v5/ws/connect) |
| Котировка | `orderbook.1.{symbol}`: на ниво 1 идват само snapshot-и, на всеки 10 ms, с повторение след 3 с без промяна; `b`/`a` = `[цена, количество]`, `ts` в ms. | [Orderbook](https://bybit-exchange.github.io/docs/v5/websocket/public/orderbook) |
| Сделки | `publicTrade.{symbol}`: `T`, `s`, `S` (страна на taker), `v`, `p`, `i`. | [Trade](https://bybit-exchange.github.io/docs/v5/websocket/public/trade) |
| Свещи | `GET /v5/market/kline`: `[start, o, h, l, c, volume, turnover]`, подредени от най-новата; последната може още да е отворена. | [Kline](https://bybit-exchange.github.io/docs/v5/market/kline) |
| Правила на двойката | `GET /v5/market/instruments-info`: `basePrecision`, `minOrderAmt`, `tickSize` (`minOrderQty` е остаряло поле). Симулацията ги взима от Bybit при старт, ако са достъпни. | [Instruments](https://bybit-exchange.github.io/docs/v5/market/instrument) |
| Подпис | HMAC-SHA256 на `timestamp+api_key+recv_window+(query или JSON тяло)`, hex с малки букви; заглавки `X-BAPI-*`. | [Guide](https://bybit-exchange.github.io/docs/v5/guide) |
| Поръчка | `POST /v5/order/create`, spot `Market`. Пазарната покупка по подразбиране е в евро, затова винаги пращаме `marketUnit=baseCoin`. `orderLinkId` е до 36 знака и е уникален (нашият UUID). `isLeverage=0`. Потвърждението е асинхронно. | [Place Order](https://bybit-exchange.github.io/docs/v5/order/create-order) |
| Статус | `GET /v5/order/realtime` (отворени и последните 500 затворени), после `GET /v5/order/history`. Едва след тях поръчката се приема за „няма я“. | [Open & Closed](https://bybit-exchange.github.io/docs/v5/order/open-order), [History](https://bybit-exchange.github.io/docs/v5/order/order-list) |
| Изпълнения и такса | `GET /v5/execution/list`. Таксата при spot покупка е в BTC, затова лотът записва нетното количество. | [Trade History](https://bybit-exchange.github.io/docs/v5/order/execution), [Enums](https://bybit-exchange.github.io/docs/v5/enum) |
| Баланс | `GET /v5/account/wallet-balance?accountType=UNIFIED`: `walletBalance`, `locked`, `spotBorrow`. | [Wallet Balance](https://bybit-exchange.github.io/docs/v5/account/wallet-balance) |
| Права на ключа | `GET /v5/user/query-api`: `readOnly`, `permissions.Spot`, `permissions.Wallet` (`Withdraw`, `AccountTransfer`), `ips`. Ключ с каквото и да е `Wallet` право се отказва. | [API Key Info](https://bybit-exchange.github.io/docs/v5/user/apikey-info) |
| Грешки | 10002 времеви прозорец, 10004 подпис, 10006 лимит на заявки, 110004 и 170131 недостатъчен баланс, 110001 и 170213 поръчката не съществува. При всеки друг код резултатът е неясен: първо се проверява статусът, едва после се изпраща повторно. | [Error Codes](https://bybit-exchange.github.io/docs/v5/error) |
| Такси Bybit EU | Spot без VIP: maker 0,10%, taker 0,25% (страницата е обновена на 08.03.2026). Симулацията използва 0,25%. | [Bybit EU Spot Fees](https://www.bybit.eu/en-EU/help-center/article/Bybit-Spot-Fees-Explained) |

**Състояние в тази версия:**
- публичните цени работят;
- проверката на ключа работи (само четене);
- балансите, поръчките и изпълненията се четат;
- **поръчки и отказ на поръчки са заключени** (`BrokerUnavailable`).

Реалната търговия изисква:
- твое изрично разрешение;
- `ALLOW_LIVE_TRADING=true`;
- малък тест край до край с реален ключ.

**Препоръчани права на ключа:**
- „API Transaction“;
- Read-Write само за Spot;
- без Wallet (теглене, прехвърляне);
- с IP ограничение.

## Kraken (резервен източник на цени)

| Тема | Проверено | Източник |
|---|---|---|
| WebSocket v2 | `wss://ws.kraken.com/v2`, канал `ticker` (`bid`, `ask`, `bid_qty`, `ask_qty`, `last`, `timestamp`, `event_trigger: bbo`) и `trade` | [Ticker](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/ticker), [Trade](https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade) |
| Свещи | `GET https://api.kraken.com/0/public/OHLC` | [OHLC](https://docs.kraken.com/api-reference/market-data/get-ohlc-data) |

Търговия през Kraken няма в тази версия.

## Robinhood (недостъпен)

„The Robinhood Crypto Trading API is available to customers in the United States only.“
В помощния център на Robinhood Europe няма API. Адаптерът отказва всяко извикване. Не се използват
недокументирани адреси и автоматичен вход с парола.
Източници: [docs.robinhood.com/crypto/trading](https://docs.robinhood.com/crypto/trading/),
[robinhood.com/eu/en/support/robinhood-crypto](https://robinhood.com/eu/en/support/robinhood-crypto/).

## TypeSafe Jev

`POST https://api.typesafe.ai/v1/systemone` с `Authorization: Bearer`, полета `state`, `model` и `questions`.
- **Въпроси:** `noul` и `choice`.
- **Грешки:** 401, 422, 429, 529.
- **Модел:** `jev-latest` → `jev-1.13.0`.
- **Цена:** $0,042 за 1M входни токена.

Jev е по-слаб с числа и сравнение на дати, затова праговете се смятат в кода.
Източник: [docs.typesafe.ai](https://docs.typesafe.ai/api).
