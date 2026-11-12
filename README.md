# 🎮 Minecraft Modpack Translation Studio (MC Translator)

> **대규모 마인크래프트 모드팩을 위한 완전 자동화 고성능 한글 번역 & 리소스팩 제작 시스템**  
> 모드 JAR, FTB 퀘스트, KubeJS, GuideMe(G키 안내서), Patchouli(인게임 책자)를 자동 탐색하여 LLM 병렬 번역, 실시간 토큰 스트리밍, 영구 아카이브($0 재사용), 모드팩 버전별 분리 내보내기 및 AGY 세션 자동 정리를 제공합니다.

---

## 🌟 주요 핵심 기능

### 1. 🔍 다중 소스 심층 스캐너 (`core/scanner.py`)
- **모드 JAR 에셋 탐색**: JAR 내부의 `assets/<mod_id>/lang/en_us.json` 및 `ko_kr.json` 자동 추출 및 분석.
- **FTB 퀘스트 완벽 지원**: `config/ftbquests/quests/chapters/*.snbt` 및 `lang/en_us.snbt`의 독자 SNBT 포맷(따옴표 없는 키, 쉼표 없는 문자열 배열, 멀티라인 텍스트 등) 완벽 파싱 및 직렬화.
- **KubeJS 스크립트 및 에셋**: `kubejs/client_scripts/`, `kubejs/assets/` 내 하드코딩된 아이템 명칭/툴팁 및 번역 파일 탐색.
- **GuideMe (G키 인게임 안내서)**: 모드 내 `assets/<mod_id>/guideme/` 폴더의 Markdown(`.md`) 파일 파싱. `<ItemLink />`, `<BlockImage />` 등 JSX/MDX 특수 태그 보존 및 번역 키 분리.
- **Patchouli (인게임 가이드북)**: `assets/<mod_id>/patchouli_books/` 내 카테고리/엔트리 JSON 파일 자동 파싱 및 `$(item)`, `$(l:...)` 매크로 보존.
- **불완전 내장 한글 필터링 (`_filter_valid_builtin_ko`)**:
  - Crowdin 등에서 영어 원문 그대로 커밋된 가짜 한글(미번역 플레이스홀더)을 자동 감지하여 미번역으로 재분류.
  - 구버전 모드팩의 깨진 인게임 책 링크(`entry://...` 레시피 누락 등) 감지 및 영문 원본(`en_flat`) 안전 폴백.

### 2. ⚡ 멀티 프로바이더 & 실시간 스트리밍 엔진 (`core/translator.py`)
- **실시간 토큰 스트리밍 (SSE / NDJSON)**:
  - **Google Gemini**: `:streamGenerateContent?alt=sse` 실시간 SSE 스트리밍 + 모델 별칭(2.5/2.0) 및 `thinkingConfig` 자동 폴백.
  - **OpenAI**: `stream: True` SSE 기반 실시간 토큰 델타 처리.
  - **Anthropic Claude**: `stream: True` SSE 기반 `content_block_delta` 실시간 수신.
  - **Ollama**: `stream: True` NDJSON 실시간 토큰 스트리밍.
  - **DeepL API**: 배치 단위 실시간 번역 진행률 연동.
- **AGY CLI (Antigravity Agent) 무제한 연동**:
  - 로컬 `agy` 프로세스 파이프를 통한 고품질 무료 번역 지원.
  - 45초 초기화 / 35초 침묵 감지 워치독 타이머 및 부분 번역 자동 복구(`extract_json_from_llm_response`).
- **🧹 AGY 세션 자동 & 수동 클린업**:
  - 번역 작업 후 생성된 임시 세션 데이터를 자동으로 선별 감지하여 안전하게 삭제.
  - SQLite DB(`conversation_summaries.db`), Protobuf 바이너리 캐시(`jetbox_summaries_proto.pb` varint 프레임 온전 보존), `brain/`, `conversations/*.db`, `history.jsonl` 완벽 정리 (일반 사용자 대화는 100% 안전 보호).

### 3. 💾 영구 번역 보관소(Mod Archive) & 번역 메모리(TM) (`core/cache.py`)
- **$0 버전 호환 재사용**: 한번 번역된 모드는 `data/mod_archive/<mod_id>.json`에 영구 보존되어, 다른 모드팩이나 새 버전에서 동일 모드 발견 시 API 비용 **$0원**으로 즉시 적용.
- **영문 원문 비교를 통한 변경 감지 (Version Changed)**:
  - 모드가 업데이트되면서 번역 키의 영문 원문(`en_text`)이 달라진 경우 이를 즉시 감지하여 '버전변경' 키로 분류 후 재번역.
- **SQLite 기반 글로벌 번역 메모리 (TM)**:
  - 전체 모드팩 전반의 고유 영문 문장(`en_text` -> `ko_text`)을 인덱싱하여 동일 문장은 단 1번만 번역.

### 4. 📦 모드팩 분리 내보내기 & Override 패키징 (`core/packager.py`)
- **모드팩 및 버전별 분리 내보내기**:
  - `exports/<모드팩명>/<버전>/` 디렉토리에 버전별로 격리 저장되어 다른 모드팩 작업물과 덮어쓰기 충돌 원천 차단.
- **배포용 3단 패키지 자동 생성**:
  - `_ResourcePack.zip`: 모드 및 패츄리 가이드북 한글 리소스팩 (`assets/<mod>/lang/ko_kr.json`).
  - `_Override.zip`: 리소스팩으로 덮어쓸 수 없는 GuideMe 책자(`kubejs/assets/`) 및 FTB 퀘스트 한글 파일.
  - `_Full_Bundle.zip`: 원클릭 배포용 통합 패키지.
  - `_Mod_Archive.json`: 번역 데이터 백업 및 공유 파일.
- **내 모드팩에 직접 적용 (Direct Apply)**:
  - 현재 로드된 모드팩 경로의 `resourcepacks/`, `config/ftbquests/quests/lang/`, `kubejs/assets/`에 즉시 설치.

### 5. 🛡️ 마인크래프트 포맷 검증 및 자동 수복 (`core/validator.py`)
- 색상 코드(`§a`, `§6`, `&a`, `&r`), 포맷 지정자(`%s`, `%d`, `%1$s`, `{0}`), GuideMe/Patchouli 특수 태그 보존.
- LLM 번역 중 누락되거나 변형된 서식을 자동으로 감지하여 원본 서식 복원.

---

## 📁 프로젝트 파일 및 폴더 구조

```text
mc_translator/
├── core/
│   ├── scanner.py          # 모드 JAR, FTBQuests, KubeJS, GuideMe, Patchouli 파서 & 유효 한글 필터
│   ├── translator.py       # 멀티 프로바이더 병렬 번역기, SSE 스트리밍 & AGY 세션 정리
│   ├── packager.py         # 리소스팩, Override.zip, Full Bundle 빌더 & 버전별 내보내기
│   ├── cache.py            # SQLite WAL 번역 메모리 & Mod Archive 영구 보관소
│   ├── cost_estimator.py   # 프로바이더별 예상 비용/시간 시뮬레이터 및 소형 모드 패킹 계산
│   ├── glossary.py         # 바닐라/모드/사용자 맞춤 4계층 용어집 관리기
│   ├── validator.py        # 마인크래프트 색상 코드, 서식 지정자 검증 및 자동 복구
│   └── __init__.py
├── data/
│   ├── translation_cache.db # SQLite 번역 캐시 및 TM 데이터베이스
│   ├── mod_archive/        # 모드별 영구 보관 JSON 파일 ($0 재사용)
│   └── agy_sandbox/        # AGY 실행용 격리 샌드박스
├── glossaries/
│   ├── vanilla.json        # 마인크래프트 공식 바닐라 한글 용어집
│   ├── global_mods.json    # 공통 모드 용어집 (에너지, 광물 등)
│   ├── mods.json           # 모드별 특화 용어집
│   └── user_custom.csv     # 사용자 정의 맞춤 용어집
├── exports/                # 모드팩별/버전별 번역 완료 패키지 보관소
├── web/
│   └── index.html          # Web Studio 단일 페이지 프론트엔드 (다크 테마 UI)
├── docs/
│   └── ARCHITECTURE.md     # 아키텍처 상세 기술 문서 및 데이터 플로우
├── main.py                 # 백엔드 서버 진입점 (HTTP REST API 및 스튜디오 호스팅)
├── CHANGELOG.md            # 기능 추가 및 변경 이력
└── README.md               # 프로젝트 종합 안내서
```

---

## 🚀 빠른 시작 (사용법)

### 1. 웹 스튜디오 실행

```bash
# 기본 실행 (브라우저 자동 열림)
python main.py --modpack "C:\Users\<사용자>\curseforge\minecraft\Instances\<모드팩폴더>"

# 포트 지정 및 백그라운드 데몬 실행
python main.py --modpack "E:\Playground\FTB Evolution 2\minecraft" --port 8765 --no-browser
```

실행 후 웹 브라우저에서 `http://127.0.0.1:8765`에 접속합니다.

### 2. 웹 스튜디오 주요 탭 구성
1. **📊 모드팩 대시보드 (`Dashboard`)**:
   - 모드팩 메타데이터 감지 (이름, 버전, 마인크래프트 버전).
   - 전체 번역률 통계, 내장 한글, 캐시 재사용, 버전 변경 키 실시간 확인.
   - 모드 목록 필터링 (미번역, 버전변경, 가이드북, 퀘스트, KubeJS 등).
   - 프로바이더 선택 (AGY CLI, Gemini 2.5/2.0 Flash, OpenAI 4o-mini, Claude 3.5 Haiku, Ollama).
   - 예상 비용 및 소요 시간 시뮬레이션.
2. **💻 번역 콘솔 (`Console`)**:
   - 병렬 워커들의 실시간 토큰 스트리밍 진행률 모니터링.
   - 방금 수신된 번역 샘플 미리보기 및 실시간 로그 확인.
3. **📖 용어집 관리 (`Glossary`)**:
   - 바닐라/모드 공통 용어집 검색 및 사용자 맞춤 CSV 용어 등록.
4. **🔍 번역 검토 (`Review`)**:
   - 모드별 원문과 번역문 대조 검토.
5. **📤 내보내기 & 보관함 (`Export History`)**:
   - 현재 모드팩 폴더에 직접 적용하기.
   - 모드팩/버전별 폴더로 분리 내보내기 및 ZIP 파일 다운로드.
   - AGY 임시 번역 세션 원클릭 수동 정리(`[🧹 지금 정리]`).

---

## ⚙️ 번역 프로바이더 설정 안내

| 프로바이더 | API 키 필요 여부 | 스트리밍 방식 | 특징 |
|---|:---:|:---:|---|
| **AGY CLI** | ❌ (로컬 구독) | JSON Stream | 고품질 한국어 번역, $0 비용 (Gemini 3.1 Pro / 4 Argon 모델 선택 가능, 비용 계산 없음), 임시 세션 자동 클린업 |
| **Google Gemini 4 Argon** | ⭕ (미입력 시 AGY 폴백) | SSE (`alt=sse`) | 차세대 플래그십, 64K 출력, In $2.00 / Out $12.00 per 1M |
| **Google Gemini 3.1 Pro** | ⭕ (미입력 시 AGY 폴백) | SSE (`alt=sse`) | 고품질 정밀 문맥 번역, 64K 출력, In $1.25 / Out $10.00 per 1M |
| **Google Gemini Flash** | ⭕ (`AI Studio`) | SSE (`alt=sse`) | 초고속(125토큰/s), 64K 출력 한도, 최저가 비용 |
| **OpenAI GPT-4o-mini** | ⭕ (`platform.openai`) | SSE (`stream: true`) | 안정적인 JSON 구조 유지 및 저렴한 비용 |
| **Anthropic Claude Haiku** | ⭕ (`console.anthropic`) | SSE (`content_block_delta`) | 자연스러운 문맥 번역 |
| **Ollama** | ❌ (로컬 호스팅) | NDJSON (`stream: true`) | 오프라인 로컬 LLM 실행 (Qwen 2.5 등) |

---

## 📄 라이선스
MIT License
