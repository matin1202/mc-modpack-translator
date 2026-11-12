# 📜 MC Translator 변경 이력 (CHANGELOG)

모든 주요 마일스톤, 신규 기능 및 버그 수정 사항의 상세 기록입니다.

---

## [v2.3.0] - 2026-10-25

### 💎 Gemini 3.1 Pro / Gemini 4 Argon 추가 & 실시간 토큰·비용 계산
- **신규 프로바이더**: `gemini_pro` (Gemini 3.1 Pro), `gemini_argon` (Gemini 4 Argon). API Key 미입력 시 로컬 AGY CLI로 자동 폴백.
- **AGY 모델 목록**에 `gemini-3.1-pro-low/medium/high` 추가.
- **모델별 단가표 (`MODEL_PRICING`)** 및 `calculate_realtime_cost()` / `estimate_text_tokens()` 도입 (한글/CJK ≈1.05 tok/자, ASCII ≈3.7자/tok).
- **실시간 입출력 토큰 집계**: 워커별 진행 중(in-flight) + 완료 토큰을 합산해 400ms 주기로 콘솔에 표시. Gemini SSE `usageMetadata`가 오면 실측값으로 대체.
- **실시간 비용**: 유료 API 엔진은 입력/출력 비용(USD/KRW) 실시간 표시. AGY/Ollama는 비용 계산 없이 "무료 ($0)"로 표기 (토큰량만 집계).
- **시뮬레이터**: 모델별 예상 비용(`model_costs`), 입력/출력 토큰 분리 표시, 모델 드롭다운에 예상 비용 표기.
- 청크 완료 로그 및 최종 요약에 토큰량·비용 포함.

---

## [v2.2.0] - 2026-10-25

### ⚡ 실시간 API 스트리밍 엔진 전면 개선
- **Google Gemini API 실시간 SSE 전환**:
  - 기존 동기 `:generateContent` 블로킹 호출 방식에서 `:streamGenerateContent?alt=sse&key=...` 실시간 스트리밍 방식으로 전면 개편.
  - 토큰 델타 도착 즉시 UI 콜백(`on_stream_delta`)을 호출하여 웹 콘솔에서 글자 단위로 실시간 확인 가능.
  - 공용 Gemini API 엔드포인트에서 지원하지 않는 `thinkingConfig` (HTTP 400) 및 모델명 404 발생 시 안전하게 2.5/2.0 Flash로 자동 폴백.
- **OpenAI / Claude / Ollama / DeepL 스트리밍 연동**:
  - OpenAI: `"stream": True` 활성화 및 SSE `choices[0].delta.content` 실시간 추출.
  - Anthropic Claude: `"stream": True` 활성화 및 SSE `content_block_delta` 실시간 추출.
  - Ollama: `"stream": False` 설정을 `"stream": True` NDJSON 실시간 토큰 스트리밍으로 전환.
  - DeepL: 50문장 배치 번역 단위 진행 상황 실시간 연동.
- **스트리밍 반응 속도 및 UI 최적화**:
  - 워커별 스트림 파싱 스로틀을 `0.7초 → 0.18초`로 단축.
  - 웹 스튜디오 상태 폴링 주기를 `800ms → 400ms`로 단축하여 지연 없는 부드러운 진행률 표시.
  - 병렬 워커 카드에 개별 워커별 수신 글자 수 및 실시간 상태 텍스트 상세 표기.

---

## [v2.1.0] - 2026-10-21

### 📚 가이드북 (GuideMe / Patchouli) 지원 & KubeJS Override 패키징
- **GuideMe 인게임 안내서 (G키 안내서) 완벽 지원**:
  - 모드 JAR 내부의 `assets/<mod>/guideme/`에 위치한 Markdown(`.md`) 파일 자동 탐색 및 파싱.
  - `<ItemLink id="..." />`, `<BlockImage ... />` 등 JSX/MDX 특수 컴포넌트 구조 온전 보존.
  - 리소스팩으로 덮어쓸 수 없는 GuideMe 구조상, `kubejs/assets/<mod>/guideme/`로 자동 배치하는 `Override.zip` 패키징 파이프라인 구축.
- **Patchouli 인게임 책자 완벽 지원**:
  - `assets/<mod>/patchouli_books/` 내 카테고리/엔트리 JSON 파일 자동 탐색 및 파싱.
  - `$(item)`, `$(l:...)`, `$(/l)` 등의 인게임 서식 매크로 완벽 보존.
- **내장 한글 유효성 필터 (`_filter_valid_builtin_ko`) 도입**:
  - Occultism 등 모드에서 Crowdin 미번역 상태로 커밋된 영문 플레이스홀더를 한글로 오인하지 않도록 자동 감지 및 필터링.
  - 구버전 모드팩에서 발생하는 깨진 `entry://...` 링크 및 누락 레시피 오류를 방지하고 영문 원본(`en_flat`)으로 안전 폴백 처리.

---

## [v2.0.0] - 2026-10-20

### 🧹 AGY CLI 임시 세션 자동 & 수동 클린업
- **계정 세션 오염 방지**:
  - `agy` CLI를 통한 대규모 번역 진행 시 생성되는 수십~수백 개의 임시 번역 대화 세션을 자동 감지.
- **정밀 바이너리 & DB 정리**:
  - `conversation_summaries.db`: 번역 세션만 안전하게 감별 삭제.
  - `jetbox_summaries_proto.pb`: Protobuf 바이너리 varint 프레임을 온전히 유지하면서 번역 세션 엔트리만 안전하게 스트립.
  - `brain/<cid>/`, `conversations/<cid>.db*`, `history.jsonl`: 관련 잔여 파일 완전 삭제.
  - 사용자의 일반 작업/대화 세션은 100% 안전 보호.
- **웹 UI 수동 정리 버튼 추가**:
  - 대시보드 및 내보내기 탭에 현재 남은 번역 세션 개수 실시간 표시 및 `[🧹 지금 정리]` 원클릭 버튼 제공.

---

## [v1.5.0] - 2026-10-19

### 📦 모드팩 분리 내보내기 & 영구 보관소 (Mod Archive)
- **모드팩 & 버전별 격리 내보내기**:
  - `exports/<모드팩명>/<버전>/` 디렉토리에 버전별로 분리 저장하여 다른 모드팩 작업물과 덮어쓰기 충돌 원천 차단.
- **배포용 3단 패키지 생성**:
  - `_ResourcePack.zip`: 모드 및 패츄리 가이드북 한글 리소스팩.
  - `_Override.zip`: GuideMe 안내서 및 FTB 퀘스트 한글 파일.
  - `_Full_Bundle.zip`: 통합 패키지.
  - `_Mod_Archive.json`: 번역 데이터 백업 파일.
- **영구 보관소(`data/mod_archive/`) & $0 비용 재사용**:
  - 번역된 모드는 모드팩 외부 영구 아카이브에 자동 저장되어, 새로운 모드팩이나 버전 업데이트 시 $0 비용으로 즉시 적용.
  - 영문 원문 비교를 통해 모드 버전 변경 시 바뀐 키만 선별하여 재번역(Version Changed 감지).

---

## [v1.0.0] - 2026-10-18

### 🚀 최초 릴리스 (MC Translator Web Studio)
- 단일 페이지 모던 웹 스튜디오 UI (`web/index.html`).
- 모드 JAR, FTB Quests SNBT, KubeJS 스크립트 스캐너.
- 동적 청크 및 LLM 프로바이더별 비용 시뮬레이터.
- 4계층 용어집 관리기 (바닐라, 공통 모드, 모드별, 사용자 맞춤 CSV).
- SQLite 기반 번역 메모리(TM) 및 마인크래프트 색상/서식 자동 검증기.
