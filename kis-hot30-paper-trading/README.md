# KIS HOT30 Paper Trading

한국투자증권 공개 종목정보와 시세 API를 결합한 **국내주식 실시간 감시·가상매매 실험 프로젝트**입니다. 실제 주문 API는 사용하지 않습니다.

## 프로젝트 목적

시가총액 후보군에서 거래가 활발한 종목을 추려 실시간 체결을 수신하고, 완성된 분봉만으로 진입 조건을 평가하는 흐름을 구현했습니다. 전략의 수익성을 입증한 서비스가 아니라 데이터 수집, 비동기 처리, 상태 복원, 오류 진단을 다루는 실험입니다.

## 처리 흐름

1. 한투 공개 KOSPI/KOSDAQ 종목정보 ZIP을 읽고, 파일 기준 시총 50~150위의 101종목을 선정합니다.
2. 한투 일봉 API로 전일까지의 최근 20거래일 평균 거래량을 계산합니다.
3. 당일 누적 거래량과 평균 거래량을 비교해 HOT 30종목을 약 10분 간격으로 갱신합니다.
4. 웹소켓으로 최대 30종목의 체결을 수신합니다. 가상 보유종목은 감시 목록에 우선 포함됩니다.
5. 당일 과거 1분봉을 보충하고 실시간 체결로 새 1분봉을 만듭니다. 완성된 1분봉 3개로 3분봉을 집계합니다.
6. 조건 충족 시 SQLite에 가상 거래를 기록하고 Discord 웹훅으로 알림을 보냅니다.

시총 순위는 종목정보 파일의 기준값이며 장중 실시간 순위가 아닙니다. 실시간인 부분은 구독 종목의 체결 수신입니다.

## 주요 구현

- `asyncio` 작업 분리: 후보 스캔, 웹소켓 수신, Discord 전송, 상태 요약
- REST 인증 토큰 재사용, 요청 타임아웃, 연결 재시도
- 미완성 3분봉 배제 및 중복/무효 분봉 처리
- 시가·고가·저가·종가·거래량 집계
- SQLite 가상 현금·포지션 복원, 거래 CSV 내보내기
- 1분 주기 요약과 데이터 부족/추세 탈락을 구분한 진단
- 공개용 패키지는 코드·설명·합성 데이터 테스트만 포함

## 현재 실험 조건

최소 완성 1분봉 30개가 있어야 합니다. 종가는 직전 10개 분봉 고가를 넘어야 하고, 직전 20개 분봉 평균 거래량은 800주 이상이어야 합니다.

| 시간대 (한국시간) | 거래량 / 직전20봉 평균 | 시가 대비 종가 상승폭 |
|---|---:|---:|
| 09:10~11:00 미만 | 1.4배 이상 | +0.15% 이상 |
| 11:00~13:00 미만 | 1.2배 이상 | +0.10% 이상 |
| 13:00~15:10 미만 | 1.3배 이상 | +0.10% 이상 |

추가로 완성 3분봉이 최소 3개이고, 마지막 3분봉이 양봉이며 종가가 최근 3봉 종가 평균 이상이어야 합니다. 최대 동시 보유는 6종목, 종목별 당일 매수는 최대 3회입니다. 가상 손절 -2%, 익절 +3.5%는 새 체결 수신 시 확인합니다. 실제 체결·청산 가격을 보장하지 않습니다.

## 실행

Python 3.11 이상이 필요합니다. 개발 환경에서는 Python 3.14.6으로 점검했습니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe bot.py --self-test
```

실제 연결을 하려면 저장소 폴더에 `.env`를 직접 만들고 다음 값을 로컬에서 입력합니다. 이 파일은 저장소에 포함하지 않습니다.

```dotenv
KIS_APP_KEY=
KIS_APP_SECRET=
DISCORD_WEBHOOK_URL=
KIS_ENV=real
```

실전용 시세 키와 Discord 웹훅이 필요합니다. KRX 로그인은 사용하지 않습니다.

```powershell
.\.venv\Scripts\python.exe bot.py
```

상세 진단은 `bot.py --verbose`, 종료는 `Ctrl+C`입니다. 같은 키로 봇을 중복 실행하지 마세요. 장외에는 대기하고, 장중에도 조건을 만족하지 않으면 매수가 없습니다.

## 검증 및 한계

공개 테스트는 합성 데이터로 분봉 집계, 미완성/누락 데이터 배제, 추세 진단, 진입 조건, 상태 요약을 확인합니다. 실제 키나 네트워크 연결 없이 실행됩니다.

개발 중 실제 저장 분봉으로 집계 결과를 독립 계산과 대조했으나 해당 데이터·개인 실행 로그는 공개하지 않습니다. 신호 빈도 비교는 수익성 백테스트가 아닙니다. 수수료·세금·슬리피지를 포함한 전략 성과는 검증하지 않았습니다.

호출 한도는 계정/정책에 따라 확인해야 합니다. 공통 유량 제한기, 중복 실행 방지, 웹소켓 구독 거절 후 복구에는 추가 개선 여지가 있습니다. 종가 강제청산은 없으며 프로그램이 꺼진 동안은 감시하지 못합니다. `--self-test` 성공은 API 통합 연결 성공을 의미하지 않습니다.

런타임에는 `.local`에 인증 캐시, 거래 DB, 분봉, CSV가 생깁니다. 파생 평균 거래량과 후보 행렬은 메모리에서 계산합니다. 실행 데이터는 자동 삭제하지 않습니다. 재실행 가능한 코드를 보존하더라도 과거 실시간 시세를 외부 API에서 항상 다시 구할 수 있는 것은 아닙니다.

## 참고한 공식 자료

- [한투 Open API 공식 예제](https://github.com/koreainvestment/open-trading-api)
- [KOSPI 종목정보 형식](https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/kis_kospi_code_mst.py)
- [KOSDAQ 종목정보 형식](https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/kis_kosdaq_code_mst.py)
- [일봉 조회](https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_stock/inquire_daily_itemchartprice/inquire_daily_itemchartprice.py)

개발 과정과 변경 이유는 [CHANGELOG.md](CHANGELOG.md)에 정리했습니다.
