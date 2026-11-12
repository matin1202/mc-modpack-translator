# 🏗️ MC Translator 시스템 아키텍처 및 기술 명세

본 문서는 `mc_translator`의 전체 내부 구조, 데이터 흐름, 모듈 간 상호작용 및 주요 알고리즘에 대한 기술적 명세를 제공합니다.

---

## 1. 전체 아키텍처 다이어그램

```
+-----------------------------------------------------------------------------------------+
|                                    Web Studio UI                                        |
|                          (HTML5 / CSS / Vanilla JS / 400ms Polling)                     |
+-----------------------------------------------------------------------------------------+
                                        |  REST API (/api/*)
                                        v
+-----------------------------------------------------------------------------------------+
|                            Backend Controller (main.py)                                 |
|            - AppController, HTTP Server, Thread-Safe Progress / Log Dispatcher          |
+-----------------------------------------------------------------------------------------+
       |                         |                          |                       |
       v                         v                          v                       v
+----------------+      +----------------+         +----------------+      +----------------+
| ModpackScanner |      | MultiProvider  |         | ModpackPackager|      |TranslationCache|
|  (scanner.py)  |      |   Translator   |         | (packager.py)  |      |   (cache.py)   |
+----------------+      | (translator.py)|         +----------------+      +----------------+
  - Mod JARs             - Multi-worker             - ResourcePack.zip      - SQLite TM (WAL)
  - FTB Quests SNBT        ThreadPool               - Override.zip          - Mod Archive
  - KubeJS client        - AGY / Gemini /           - Full_Bundle.zip         (data/mod_archive)
  - GuideMe MD             OpenAI / Claude /        - Direct Apply to       - Version Changed
  - Patchouli JSON         Ollama Streaming           Modpack Directory       Detection
  - Built-in KO filter   - Watchdog Timer
                         - AGY Cleanups
```

---

## 2. 모듈별 상세 역할 및 명세

### 2.1 스캐너 모듈 (`core/scanner.py`)
- **ModEntry 데이터 모델**:
  - `entry_id`: 모드 식별 고유 ID (예: `jar:botania`, `ftbquests:chapter:tech`, `guideme:occultism`)
  - `source_type`: `mod_jar`, `ftbquests`, `kubejs`, `guideme`, `patchouli`
  - `en_flat`: 평탄화된 영어 원본 딕셔너리 (`{ "key": "English text" }`)
  - `ko_flat`: 모드에 내장된 유효 한국어 번역 딕셔너리
  - `cached_map`: 기존 DB 및 아카이브에서 로드된 번역 딕셔너리
- **SNBT 파서 (`parse_snbt_lang`, `serialize_snbt_lang`)**:
  - FTB Quests 특유의 따옴표 없는 키(`quest.0001.title:`), 쉼표 생략, 문자열 배열(`[ "라인1" "라인2" ]`) 완벽 파싱 및 원본 서식 유지 직렬화.
- **GuideMe 마크다운 파서 (`scan_guideme_books`, `reconstruct_guideme_markdown`)**:
  - G키 안내서 Markdown에서 줄바꿈 및 JSX 태그(`<ItemLink id="..." />`, `<RecipeFor ... />`)의 위치를 유지하며 번역 대상 텍스트만 청크 키로 추출 및 복원.
- **유효 내장 한글 필터 (`_filter_valid_builtin_ko`)**:
  - Crowdin의 자동 커밋 등으로 인해 `ko_kr.json`에 영문 원문이 그대로 들어간 키를 검출하여 미번역으로 분류.
  - 가이드북 내 깨진 `entry://` 또는 누락된 레시피 키에 대한 런타임 오류 방지.

### 2.2 번역 엔진 및 스트리밍 모듈 (`core/translator.py`)
- **스마트 교차 모드 배칭 (`pack_small_mods`)**:
  - 수십 개의 소형 모드(키 수가 적은 모드)를 단일 LLM 요청 청크(최대 300~2500키)로 묶어 LLM 시스템 프롬프트 오버헤드 80% 이상 절감.
- **멀티 워커 병렬 실행 (`ThreadPoolExecutor`)**:
  - 사용자 설정에 따라 1~12개의 워커 스레드가 독립적으로 청크를 번역 및 스트리밍.
- **실시간 토큰 스트리밍**:
  - **Gemini**: `:streamGenerateContent?alt=sse`
  - **OpenAI**: `stream: True` SSE
  - **Claude**: `stream: True` SSE
  - **Ollama**: `stream: True` NDJSON
  - 워커별로 수신되는 토큰 델타를 누적하며 `on_stream_delta`를 0.18초 주기로 호출하여 실시간 수신 글자 수 및 미리보기 갱신.
- **워치독 타이머 및 부분 복구**:
  - 초기화 타임아웃(45초) 및 무응답 타임아웃(35초) 적용.
  - 네트워크 단절이나 타임아웃 발생 시, 그때까지 수신된 텍스트에서 `extract_json_from_llm_response`로 완성된 키들을 구조화하여 먼저 저장하고 남은 키만 재시도.
- **AGY CLI 세션 클린업 (`cleanup_agy_sessions`)**:
  - `~/.gemini/antigravity-cli/conversation_summaries.db`, `jetbox_summaries_proto.pb`, `conversations/*.db`, `brain/`, `history.jsonl`에서 모드팩 번역용 임시 세션만 정확히 감별하여 삭제. 일반 사용자의 코딩/대화 세션은 무손상 보존.

### 2.3 패키저 모듈 (`core/packager.py`)
- **모드팩 식별 및 버전 격리 (`resolve_export_identity`)**:
  - 모드팩 메타데이터(`manifest.json`, `instance.cfg`, `minecraftinstance.json` 등)에서 모드팩 이름과 버전을 감지.
  - `exports/<모드팩명>/<버전>/` 디렉토리에 분리 저장.
- **패키지 구성**:
  1. `ResourcePack.zip`:
     - `assets/<mod_id>/lang/ko_kr.json`
     - `assets/<mod_id>/patchouli_books/.../ko_kr/...`
     - `pack.mcmeta` (모드팩 이름/버전 포함)
  2. `Override.zip`:
     - `kubejs/assets/<mod_id>/guideme/...` (GuideMe 마크다운 파일)
     - `config/ftbquests/quests/lang/ko_kr.snbt`
  3. `Full_Bundle.zip`: 리소스팩과 오버라이드가 결합된 원클릭 압축본.
  4. `Mod_Archive.json`: 번역된 모드별 키-값 덤프 (백업 및 공유용).
- **직접 적용 (`apply_to_modpack`)**:
  - 대상 모드팩 폴더의 실제 경로에 리소스팩 복사, 퀘스트 SNBT 저장, KubeJS 폴더에 GuideMe 파일 자동 배치.

### 2.4 캐시 및 번역 메모리 모듈 (`core/cache.py`)
- **SQLite DB (`data/translation_cache.db`)**:
  - 테이블: `translations` (`entry_id`, `lang_key`, `en_text`, `ko_text`, `provider`, `updated_at`)
  - 인덱스: `idx_en_lookup` (`en_text`) - 글로벌 고유 문장 번역 메모리 매칭 속도 극대화.
- **영구 보관소 (`data/mod_archive/<mod_id>.json`)**:
  - SQLite와 별개로 모드별 JSON 파일로 영구 보관.
  - 다른 PC로 이동하거나 다른 모드팩을 번역할 때 모드 ID만 일치하면 API 호출 없이 100% 자동 재사용.

### 2.5 마인크래프트 포맷 검증기 (`core/validator.py`)
- 마인크래프트 전용 서식 검증:
  - 색상 접두사/접미사 (`§a`, `§6`, `&a`, `&r` 등) 누락 시 영문 원본 기준으로 자동 복원.
  - C 스타일 포맷 (`%s`, `%d`, `%1$s`, `%.2f` 등) 순서 및 개수 보존 검사.
  - Java MessageFormat (`{0}`, `{1}`, `{@pagebreak}` 등) 태그 보존 검사.
  - GuideMe/Patchouli 특수 링크 태그 손상 방지.

---

## 3. 데이터 흐름 (Data Flow)

```
[모드팩 스캔]
   │
   ├─► Mod JARs, FTB Quests, KubeJS, GuideMe, Patchouli 탐색
   │
   ├─► 유효 내장 한글 확인 & 불완전 한글 필터링
   │
   ├─► 영구 보관소(Mod Archive) 및 캐시와 영문 원문 대조
   │      ├─ 일치: $0 캐시 즉시 재사용
   │      ├─ 영문 변경됨: '버전변경' 키로 분류하여 재번역 큐 추가
   │      └─ 미번역: 번역 큐 추가
   │
[LLM 번역 실행]
   │
   ├─► 중복 제거 & 글로벌 TM(Translation Memory) 매칭
   │
   ├─► 스마트 교차 모드 청크 패킹 (max 300~2500키)
   │
   ├─► 병렬 워커 스레드 할당 (1~12 Workers)
   │      │
   │      ├─► LLM 실시간 SSE/NDJSON 스트리밍 (Gemini / OpenAI / Claude / Ollama / AGY)
   │      ├─► 0.18s 주기로 웹 콘솔에 글자 단위 실시간 반영
   │      └─► 워치독 모니터링 (이상 발생 시 부분 복구 & 재시도)
   │
   ├─► 마인크래프트 포맷 자동 복구 (Validator)
   │
   ├─► SQLite DB 및 Mod Archive 즉시 영구 저장
   │
[패키징 및 배포]
   │
   ├─► ResourcePack.zip / Override.zip / Full_Bundle.zip 생성
   │
   ├─► exports/<모드팩>/<버전>/ 분리 보관
   │
   └─► 모드팩 폴더에 직접 적용 (Direct Apply)
```
