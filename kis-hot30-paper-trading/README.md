# KIS HOT30 Paper Trading

한국투자증권 시세로 국내주식 30종목을 실시간 감시하고 조건이 맞으면 가상매매를 기록하는 Python 봇입니다. Discord 알림을 지원하며 **실계좌 주문 기능은 없습니다**.

[코드](bot.py) · [테스트](tests/test_bot.py) · [개발 기록](CHANGELOG.md)

## 동작 방식

| 단계 | 처리 |
|---|---|
| 후보 선정 | 한투 KOSPI/KOSDAQ 종목정보 파일에서 시총 50~150위, 101종목 선정 |
| 거래량 비교 | 전일까지 최근 20거래일 평균과 당일 누적 거래량 비교 |
| 감시 목록 갱신 | 약 10분마다 HOT 30종목 선정, 가상 보유종목 우선 포함 |
| 시세 수신 | 웹소켓으로 최대 30종목의 실시간 체결 수신 |
| 분봉 생성 | 당일 과거 1분봉 보충, 실시간 1분봉 생성, 완성된 3개 분봉을 3분봉으로 집계 |
| 가상매매 | 조건 충족 시 SQLite에 거래 기록, Discord 알림 전송 |

시총 순위는 종목정보 파일의 기준값입니다. 장중 실시간으로 받는 데이터는 구독 종목의 체결입니다.

후보 조회, 체결 수신, 알림 전송, 상태 요약은 `asyncio` 작업으로 나눴습니다. 인증 토큰 재사용, 요청 타임아웃, 연결 재시도를 처리하고, 재시작 시 SQLite에서 가상 현금과 포지션을 복원합니다.

## 실행

Python 3.11 이상을 사용합니다. 개발 환경에서는 Python 3.14.6으로 점검했습니다.

이 저장소를 내려받은 뒤 **`kis-hot30-paper-trading` 폴더 안에서** 다음 명령을 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe bot.py --self-test
```

실시간 연결에는 한투 실전용 시세 키와 Discord 웹훅이 필요합니다. `bot.py`와 같은 폴더에 `.env`를 만들고 값을 입력합니다. KRX 로그인은 사용하지 않습니다.

```dotenv
KIS_APP_KEY=
KIS_APP_SECRET=
DISCORD_WEBHOOK_URL=
KIS_ENV=real
```

`.env`는 로컬에만 보관합니다.

```powershell
.\.venv\Scripts\python.exe bot.py
```

기본 화면에는 1분마다 수신 상태, 가상 보유, 실현손익, 조건별 판정 수가 표시됩니다. 상세 로그는 `bot.py --verbose`, 종료는 `Ctrl+C`입니다. 같은 키로 중복 실행하지 마세요.

## 진입 조건

완성 1분봉이 최소 30개 필요합니다. 종가가 직전 10개 분봉 고가를 넘고, 직전 20개 분봉의 평균 거래량이 800주 이상이어야 합니다.

| 시간대·한국시간 | 거래량 / 직전 20봉 평균 | 시가 대비 종가 상승폭 |
|---|---:|---:|
| 09:10 이상, 11:00 미만 | 1.4배 이상 | +0.15% 이상 |
| 11:00 이상, 13:00 미만 | 1.2배 이상 | +0.10% 이상 |
| 13:00 이상, 15:10 미만 | 1.3배 이상 | +0.10% 이상 |

3분봉은 완성된 봉이 최소 3개여야 합니다. 마지막 봉이 양봉이고 종가가 최근 3봉 종가 평균 이상일 때 통과합니다. 장외에는 대기하고, 장중에도 조건을 충족하지 않으면 매수하지 않습니다.

## 보유·청산 규칙

| 항목 | 설정 |
|---|---:|
| 최대 동시 보유 | 6종목 |
| 종목별 당일 매수 | 최대 3회 |
| 가상 손절 | −2% |
| 가상 익절 | +3.5% |

손절·익절은 새 체결을 받을 때 확인합니다. 종가 강제청산은 없으며 프로그램이 꺼진 동안은 감시하지 못합니다. 실제 거래에서 같은 가격에 체결될 것을 보장하지 않습니다.

## 검증과 남은 작업

합성 데이터 테스트로 분봉 집계, 미완성·누락·중복 데이터 처리, 추세 판정, 진입 조건, 상태 요약을 확인합니다. 테스트에는 실제 키나 네트워크 연결이 필요하지 않습니다. `--self-test`는 API 연결 검증과 별도입니다.

개발 중 저장 분봉의 OHLCV를 독립 계산과 대조했습니다. 해당 분봉과 개인 실행 로그는 공개하지 않습니다.

- **성과 검증:** 신호 빈도만 비교했습니다. 수수료·세금·슬리피지를 포함한 수익성은 검증하지 않았습니다.
- **호출 제어:** 계정별 API 한도 확인과 공통 요청 속도 제한이 필요합니다.
- **운영 개선:** 중복 실행 방지, 웹소켓 구독 거절 후 복구가 남아 있습니다.

## 로컬 저장 데이터

실행하면 `.local/`에 인증 캐시, 거래 DB, 분봉, 거래 CSV가 생성됩니다. 평균 거래량과 후보 행렬은 메모리에서 계산합니다. 실행 데이터는 자동 삭제하지 않습니다.

인증정보, 개인 실행 로그, 거래내역, 저장 분봉은 공개 대상이 아닙니다. 과거 실시간 시세는 외부 API에서 항상 다시 구할 수 있는 것은 아닙니다.

## 공식 자료

- [한투 Open API 예제](https://github.com/koreainvestment/open-trading-api)
- [KOSPI 종목정보 형식](https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/kis_kospi_code_mst.py)
- [KOSDAQ 종목정보 형식](https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/kis_kosdaq_code_mst.py)
- [일봉 조회 예제](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py)
