# 김종엽 · Jongyeop Kim

금융 시계열과 투자전략을 연구합니다. 예측 신호의 통계적 유의성과 거래비용을 반영한 성과를 함께 살펴봅니다.

## 연구

### [KOSPI200 단기반전과 PCA](https://github.com/Kimjongyeop-web/kospi200-pca-reversal)
KOSPI200 구성종목의 단기반전 신호를 검정하고, PCA로 공통요인을 제거했을 때 예측력이 나아지는지 비교했습니다. 단기반전은 관찰됐지만 PCA의 추가 개선은 확인되지 않았습니다.

TEAM Beta 공동연구 · 김종엽, 오준영, 장민준, 황성혁  
[발표 자료](https://github.com/Kimjongyeop-web/kospi200-pca-reversal/blob/main/PRESENTATION.md) · [코드와 결과](https://github.com/Kimjongyeop-web/kospi200-pca-reversal)

### [Bitcoin 변동성 예측](https://github.com/Kimjongyeop-web/bitcoin-volatility-research)
BTC/KRW 분봉으로 실현변동성을 계산하고 HAR와 HAR-X를 비교했습니다. 거래량·하락 변동성 정보를 추가했을 때 예측 오차가 소폭 줄었으나, 통계적으로 뚜렷한 개선은 확인하지 못했습니다.

[보고서](https://github.com/Kimjongyeop-web/bitcoin-volatility-research/blob/main/REPORT.md) · [코드와 결과](https://github.com/Kimjongyeop-web/bitcoin-volatility-research)

### [에너지 시장 뉴스 위험과 DGRI](https://github.com/Kimjongyeop-web/commodity-risk-dgri)
GDELT 뉴스로 원유·천연가스·전력의 위험지수를 만들고 시장 간 연결성을 분석했습니다. 30일 이동창으로 위험 전달 방향의 변화를 살펴봤습니다.

POSTECH AIRM Research Group 공동연구 · 김종엽, 배현우  
[발표 자료](https://github.com/Kimjongyeop-web/commodity-risk-dgri/blob/main/PRESENTATION.md) · [코드와 결과](https://github.com/Kimjongyeop-web/commodity-risk-dgri)

## 진행 중

### [52주 신고가와 베이지안 비중 배분](https://github.com/Kimjongyeop-web/bayesian-52week-high)
신고가 근접도로 종목을 고르고, 베이지안 추정의 불확실성을 종목 비중에 반영하는 실험입니다. 보유기간·일별 재배분·회전율 제어를 비교하고 있습니다.

[실험 현황과 남은 검증](https://github.com/Kimjongyeop-web/bayesian-52week-high/blob/main/IN_PROGRESS.md)

## 데이터 수집·매매 도구

| 프로젝트 | 구현 내용 |
|---|---|
| [Market Data API](https://github.com/Kimjongyeop-web/market-data-api) | Upbit 일봉·분봉 수집, Naver 종목별 가격 이력 보완 |
| [KIS HOT30 Paper Trading](kis-hot30-paper-trading/) | 한투 실시간 체결 수신, 1·3분봉 생성, 조건 기반 가상매매와 Discord 알림 |

KIS 봇은 가상매매용이며 실계좌 주문 기능은 없습니다. 각 저장소에서 연구 설계, 실행 조건과 검증 범위를 확인할 수 있습니다.

[공개 자료와 데이터 범위](DATA_POLICY.md)
