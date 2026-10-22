# Mini Text-to-SQL Agent

以中文提問，查詢 PostgreSQL 資料庫的本機資料查詢工作台。應用先透過 Request Guard 攔截明確的新增、刪除、修改及資料表結構異動要求，通過後才生成 SQL、檢查查詢範圍並以唯讀交易執行。遇到可修正的 SQL 錯誤時最多修正三次，最後以繁體中文標籤呈現結果。

這是教學與作品展示用的 MVP，主要展示 LangGraph 的狀態管理、條件路由及 SQL 修正流程。查詢對象不限 AQI；設定白名單與適當權限後，即可使用自己的資料表。

**每次提問的執行路徑：** Gradio 介面 → LangGraph（`schema_loader` 內先執行 Request Guard）→ LLM 產生 SQL → SQLGlot 驗證 → SQLAlchemy／psycopg 唯讀查詢 → Python 格式化結果。資料異動要求在 Guard 階段直接轉入固定回覆，不進入模型生成與 SQL 執行。

## 目錄

- [功能總覽](#功能總覽)
- [快速開始](#快速開始)
- [環境變數](#環境變數)
- [PostgreSQL 資料與權限準備](#postgresql-資料與權限準備)
- [介面操作](#介面操作)
- [SQL 支援範圍](#sql-支援範圍)
- [LangGraph 工作流程](#langgraph-工作流程)
- [Request Guard 資料異動攔截](#request-guard-資料異動攔截)
- [資料庫執行與結果呈現](#資料庫執行與結果呈現)
- [專案結構](#專案結構)
- [測試與驗證](#測試與驗證)
- [常見問題](#常見問題)
- [已知限制](#已知限制)

## 功能總覽

| 功能 | 實際行為 |
| --- | --- |
| 中文轉 SQL | 透過單一設定的 Ollama 模型，根據問題及資料表 Schema 產生 PostgreSQL SELECT |
| 查詢白名單 | 只允許 `ALLOWED_TABLES` 內的實體資料表；可在白名單表之間 JOIN |
| 資料異動攔截 | 明確新增、刪除、修改資料或變更資料表結構的要求，直接回覆唯讀限制，不呼叫模型、不執行 SQL，也不進入自動修正 |
| Schema 載入 | 啟動時讀取欄位名稱與型別，檢查帳號權限並快取；不依賴預寫的 AQI 欄位 |
| SQL 驗證 | 使用 PostgreSQL 方言解析 AST，限制語句、資料來源、函式與型別，重新產生 SQL 後才執行 |
| SQL 自動修正 | 將問題、Schema、失敗 SQL 與整理後的錯誤交給同一個模型，最多修正三次 |
| 唯讀執行 | 每次資料庫操作使用唯讀交易、SQL／鎖定逾時與受限的 `search_path` |
| 結果限制 | 預設最多顯示 100 筆；額外讀一筆判斷是否截斷，並明確提示 |
| 結果與 SQL 檢視 | 分頁顯示文字結果、最後一版 SQL，以及成功／失敗和修正次數 |
| 資料表清單 | 可依 Schema 或表名搜尋、捲動及複製完整表名 |
| 獨立提問 | 每次查詢重新建立狀態，不保留前次問題、SQL 或修正次數 |
| 本機工作台 | 支援窄螢幕與深淺色外觀；預設只綁定 `127.0.0.1`，不開啟公開分享 |

**只有 SQL 生成與修正會呼叫 LLM。** 查詢結果由 Python 格式化，不另交給模型摘要。使用雲端模型時，問題、白名單 Schema，以及修正時的 SQL 與錯誤資訊仍會傳到模型服務。

Request Guard 的呼叫位置、比對規則與案例見[資料異動攔截說明](#request-guard-資料異動攔截)。它處理輸入問題（含直接輸入的 SQL），SQL 驗證器處理模型輸出，PostgreSQL 唯讀交易與權限則限制實際執行。

## 快速開始

### 1. 準備執行環境

- Python：本次驗證使用 Windows、Python **3.12.7**；其他版本尚未在本專案驗證。
- PostgreSQL：準備好可連線的資料庫、既有資料表與符合下方要求的唯讀帳號。
- 模型：預設使用 Ollama Cloud，需要 API Key；也可使用自行啟動的本機 Ollama。

介面使用 Gradio，無須另外安裝 Node.js 或啟動前端服務。直接相依版本固定於 [requirements.txt](requirements.txt)，間接相依由 pip resolver 決定。

### 2. 安裝套件

以下 PowerShell 指令從儲存庫根目錄執行，在 `text2sql` 內建立新的虛擬環境：

```powershell
cd text2sql
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

若已有 `.env`，請直接編輯，不要覆蓋既有設定。macOS／Linux 對應指令：

```bash
cd text2sql
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp -n .env.example .env
```

後續指令皆假設位於 `text2sql`，並使用這裡建立的 `.venv`。若沿用儲存庫根目錄的虛擬環境，PowerShell 的 Python 路徑改成 `..\.venv\Scripts\python.exe`。

### 3. 編輯設定並準備資料庫

將 `.env` 改成自己的連線資訊，例如：

```dotenv
LLM_MODEL=gpt-oss:120b
OLLAMA_BASE_URL=https://ollama.com
OLLAMA_API_KEY=填入你的_Ollama_API_Key
LLM_TIMEOUT_SECONDS=120
DATABASE_URL=postgresql+psycopg://text2sql_reader:填入密碼@localhost:5432/query_demo
ALLOWED_TABLES=public.air_quality_hourly,public.air_quality_predictions,public.air_quality_predictions_8h
STATEMENT_TIMEOUT_MS=5000
MAX_RESULT_ROWS=100
```

此處使用既有的專用示範資料庫 `query_demo`；[.env.example](.env.example) 原始範本的資料庫名稱是 `postgres`，請依實際環境替換。密碼若包含 `@`、`:`、`/` 等保留字元，須先 URL encode。

三張 AQI 表只是範本，**專案沒有附建表、匯入或種子資料腳本**。請先備妥資料，或將白名單替換成自己的表，並完成[唯讀權限設定](#postgresql-資料與權限準備)。

### 4. 啟動

```powershell
.\.venv\Scripts\python.exe app.py
```

開啟終端顯示的網址，通常為 `http://127.0.0.1:7860`。若該連接埠已被占用，以實際顯示的 URL 為準。macOS／Linux 使用 `.venv/bin/python app.py`。

初始化成功後可送出查詢；失敗時介面仍會開啟、顯示錯誤並停用送出按鈕。修正設定或資料庫權限後須重新啟動。

> 頁首「可開始查詢」表示設定與 Schema 初始化完成。模型在第一次提問時才會被呼叫，因此這個狀態不代表 API Key、模型可用性或額度已通過實際連線測試。

## 環境變數

設定由 `config.py` 載入與檢查；固定讀取 **`text2sql/.env`**，既有的作業系統環境變數優先於檔案內容。

| 變數 | 必填／預設值 | 格式與用途 |
| --- | --- | --- |
| `LLM_MODEL` | 必填 | Ollama 模型完整名稱，須與目標服務提供的名稱一致 |
| `OLLAMA_BASE_URL` | 預設 `https://ollama.com` | `http(s)://主機[:連接埠]`；不得包含 `/api`、`/v1`、帳密、查詢參數或 fragment；尾端 `/` 會移除 |
| `OLLAMA_API_KEY` | 連到 `ollama.com` 時必填 | 以 Bearer header 傳送；本機服務可留空；連到 `ollama.com` 必須使用 HTTPS |
| `LLM_TIMEOUT_SECONDS` | 預設 `120` | 整數，1～600 秒；用於模型 HTTP 操作的逾時，不是整個查詢流程的總時限 |
| `DATABASE_URL` | 必填 | 必須使用 `postgresql+psycopg://` 驅動格式 |
| `ALLOWED_TABLES` | 必填 | 以逗號分隔的完整小寫 `schema.table` 名稱；去除項目前後空白及重複項目 |
| `STATEMENT_TIMEOUT_MS` | 預設 `5000` | 整數，1～60000 毫秒；也套用於啟動時的 Schema 讀取 |
| `MAX_RESULT_ROWS` | 預設 `100` | 整數，1～1000；單次最多顯示的資料筆數 |

白名單每段名稱須符合 `[a-z_][a-z0-9_]*`；不接受需要引號的大小寫、空白或特殊字元名稱。不可使用 `information_schema` 或任何以 `pg_` 開頭的 Schema，也不可留下空項目或尾端逗號。

`.env` 已被根目錄的 `.gitignore` 排除；只有 `.env.example` 是可分享的設定範本。

### Ollama Cloud

先在 [Ollama API Keys](https://ollama.com/settings/keys) 建立金鑰，再設定 `.env`。本專案透過 `ChatOllama` 直連原生 API，不需要本機 Ollama、`ollama signin` 或 `OPENAI_API_KEY`。直連網址與模型名稱的用法見 [Ollama Cloud 官方文件](https://docs.ollama.com/cloud)。

可查詢目前的雲端模型清單，再將回傳名稱填入 `LLM_MODEL`：

```powershell
Invoke-RestMethod https://ollama.com/api/tags |
    Select-Object -ExpandProperty models |
    Select-Object name
```

範本中的 `gpt-oss:120b` 以及測試使用的 `nemotron-3-super` 都是設定示例，不保證服務持續提供；以 API 清單和帳號可用範圍為準。切換模型只需修改 `LLM_MODEL` 並重啟，沒有多模型路由或 Fallback。

若要獨立確認模型連線，在 `text2sql` 目錄啟動 Python 後執行以下內容；這會實際呼叫模型並使用帳號額度：

```python
from config import create_llm, load_settings

llm = create_llm(load_settings())
print(llm.invoke("只回答 OK").content)
```

`load_settings()` 仍要求資料庫 URL 與白名單格式完整，但這段測試不會連線資料庫。應用只把模型的最終字串 `content` 當成 SQL；獨立的 `thinking` 欄位不會拿來執行。

### 本機 Ollama

先自行啟動 Ollama 並準備模型，再修改：

```dotenv
OLLAMA_BASE_URL=http://localhost:11434
LLM_MODEL=填入本機已安裝的完整模型標籤
OLLAMA_API_KEY=
```

其他資料庫與限制設定仍需保留。若系統環境變數中已有 `OLLAMA_API_KEY`，須一併清除或調整，因為它會優先於 `.env`。

## PostgreSQL 資料與權限準備

### 資料需求與啟動檢查

應用只讀取既有的一般表或分割表，不會建立資料庫、角色、資料表或修改授權。所有白名單表都必須存在，任一張表檢查失敗就不建立查詢流程。

| 檢查項目 | 啟動要求 |
| --- | --- |
| 登入角色旗標 | 不得具備 superuser、CREATEROLE、CREATEDB、REPLICATION 或 BYPASSRLS |
| 目前資料庫有效權限 | 不得有 `CREATE` 或 `TEMP`／`TEMPORARY` |
| 白名單資料表 | 必須是一般表或分割表，且可讀到欄位中繼資料 |
| 表層級有效權限 | 必須可 `SELECT`；不得有 `INSERT`、`UPDATE`、`DELETE`、`TRUNCATE`、`REFERENCES`、`TRIGGER` |
| 白名單所屬 Schema | 不得有 `CREATE`；查詢所需的 `USAGE` 應由管理員授予 |

Schema 快取僅包含表名、欄位名與型別；不包含範例資料、主外鍵關係、欄位註解或業務單位。修改白名單或資料表結構後，須重新啟動才會更新快取。

### 專用示範資料庫的設定範例

以下由管理員連入**已存在的 `query_demo`** 後執行，假設三張 AQI 表已存在。`DATABASE_URL` 的資料庫名稱也必須一致。範例會撤銷 `PUBLIC` 的權限，會影響同一資料庫的其他帳號，適用於專用示範環境。

```sql
-- 僅在角色尚未建立時執行；登入者不可是資料表或資料庫擁有者。
CREATE ROLE text2sql_reader LOGIN
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
-- 在 psql 另行執行 \password text2sql_reader，互動設定密碼。

REVOKE ALL ON DATABASE query_demo FROM PUBLIC;
GRANT CONNECT ON DATABASE query_demo TO text2sql_reader;

REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO text2sql_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM PUBLIC;
REVOKE EXECUTE ON ALL ROUTINES IN SCHEMA public FROM PUBLIC;

GRANT SELECT ON TABLE
    public.air_quality_hourly,
    public.air_quality_predictions,
    public.air_quality_predictions_8h
TO text2sql_reader;

ALTER ROLE text2sql_reader SET default_transaction_read_only = on;
ALTER ROLE text2sql_reader SET statement_timeout = '5s';
ALTER ROLE text2sql_reader SET search_path = pg_catalog;
```

使用其他表或 Schema 時，請同步替換授權內容與 `ALLOWED_TABLES`。既有帳號可能仍有直接授權、角色成員資格、擁有權或其他 Schema 的能力，不能只重跑 `GRANT SELECT` 就視為符合要求。

PostgreSQL 的有效權限可能來自 `PUBLIC` 或其他角色；只對使用者執行 `REVOKE` 不會移除這些來源的權限。相關規則見 [PostgreSQL 權限文件](https://www.postgresql.org/docs/current/ddl-priv.html)。

### 新增可查詢資料表

1. 由管理員準備既有表，並授予登入者該 Schema 的 `USAGE` 及該表的 `SELECT`。
2. 將完整表名加入 `.env` 的 `ALLOWED_TABLES`。
3. 重新啟動，讓應用重新檢查權限與載入 Schema。

資料庫授權與應用白名單是兩層限制；只修改其中一層不足以讓新表可用。

若在專用 Schema 使用 `GRANT SELECT ON ALL TABLES IN SCHEMA public TO text2sql_reader;`，只會授權當下既有的表。未來物件的授權由物件建立者的 `ALTER DEFAULT PRIVILEGES` 控制，仍不會自動加入應用白名單；詳見 [PostgreSQL 預設權限文件](https://www.postgresql.org/docs/current/sql-alterdefaultprivileges.html)。

## 介面操作

1. 確認頁首顯示「可開始查詢」。側欄列出設定的白名單表；啟動失敗時，這份清單不代表所有表都已驗證通過。
2. 在「資料範圍」搜尋表名或 Schema，必要時按複製按鈕取得完整名稱。搜尋不區分大小寫，僅篩選表名，不查詢資料內容或欄位。
3. 輸入問題，或點選範例後修改 `xx資料表`／`xx欄位`。範例只帶入文字，不會自動送出。
4. 按「送出查詢」，查看「查詢結果」及下方狀態；可使用結果框的複製功能。
5. 切換「SQL 檢視」審閱 SQL。成功時顯示實際執行版本；失敗時顯示最後保留的版本，可能尚未通過驗證或執行。

可先使用不依賴特定欄位的問題：

```text
請顯示 public.air_quality_hourly 的前 5 筆資料。
請統計 public.air_quality_hourly 總共有幾筆資料。
```

若自己的表確實具有 `observed_at` 與 `aqi` 欄位，也可詢問：

```text
請依月份統計 2025 年的平均 AQI，按月份排序。
請計算 2025 年 AQI 超過 100 的資料筆數。
請列出 2025 年每個月 AQI 的中位數。
```

表名、欄位及日期範圍均須依實際資料調整；應用沒有內建 AQI 題目對應 SQL。每次問題互相獨立，不能用「那上一題呢」延續上下文。查詢按鈕事件的同時執行數設定為 1，後續請求由 Gradio 佇列處理。

複製表名優先使用瀏覽器 Clipboard API，失敗時嘗試相容方式；仍失敗會顯示手動複製提示。介面支援鍵盤焦點及複製狀態提示，窄螢幕會改成單欄。

## SQL 支援範圍

驗證器採**明確允許的 AST 節點清單**；即使是合法 PostgreSQL 語法，只要未支援也會拒絕。

| 類型 | 支援內容 |
| --- | --- |
| 查詢結構 | 單一 SELECT、非遞迴 `WITH ... SELECT`、子查詢、JOIN、DISTINCT |
| 篩選與統計 | WHERE、GROUP BY、HAVING、ORDER BY、CASE、常見比較／邏輯／算術、IN、BETWEEN、LIKE／ILIKE |
| 分頁 | 常數 LIMIT／OFFSET，必須是非負整數 |
| 聚合 | COUNT、SUM、AVG、MIN、MAX、PERCENTILE_CONT、PERCENTILE_DISC |
| 日期 | EXTRACT、DATE_TRUNC，以及受限的日期時間型別與單位 |
| 數字／空值 | CAST、ROUND、ABS、COALESCE、NULLIF |
| 文字 | LOWER、UPPER、LENGTH |

不支援寫入或管理指令、多條語句、寫入型 CTE、遞迴 CTE、UNION、視窗函式、`SELECT INTO`、鎖定查詢、資料表函式、任意自訂函式、Schema 限定的函式及自訂型別。一般 View、物化 View 與外部表也不支援啟動載入。

驗證細節：

- SQL 不得為空，長度最多 20000 字元；Markdown 程式碼圍欄不會自動移除，而是交由驗證／修正流程處理。
- 實體表須符合白名單；未寫 Schema 時，只有唯一匹配才會自動補上，名稱有歧義則拒絕。
- 會按 CTE／子查詢的作用域檢查來源，區分 CTE 別名與真正資料表；禁止跨資料庫限定名稱。
- 未加引號的識別字轉成小寫；加引號者保留大小寫，仍須符合白名單。
- 通過後移除註解並重新產生 PostgreSQL SQL，執行的是這份 SQL。
- 驗證器不完整解析欄位是否存在、型別及聚合語意；相關錯誤由 PostgreSQL 回報後再決定是否修正。

## LangGraph 工作流程

流程以 LangGraph 的 `StateGraph(AgentState)` 建立，仍使用六個節點。每次提問先由 `initial_state()` 建立新的 State，進入 `schema_loader` 後，第一步呼叫 `request_guard.is_write_request()`。被攔截時直接轉入 `answer_generator`；通過後才檢查初始化狀態、注入快取 Schema，再進入 SQL 生成、驗證、執行及修正流程。

下圖將 `schema_loader` 的內部步驟展開，**Request Guard 是該節點呼叫的函式，不是第七個 LangGraph 節點**。`repair_check` 同樣代表條件路由判斷，不是額外註冊的節點。

```mermaid
flowchart TD
    start([START]) --> guard
    subgraph schema_loader["schema_loader：輸入檢查與快取 Schema"]
        guard{"Request Guard<br/>是否要求新增、刪除、修改<br/>資料或資料表結構？"}
        schema_check["檢查空問題與初始化狀態<br/>注入快取 Schema"]
        guard -->|否| schema_check
    end
    guard -->|是：request| answer_generator
    schema_check -->|成功| sql_generator
    schema_check -->|失敗：initialization| answer_generator
    sql_generator -->|取得 SQL| sql_validator
    sql_generator -->|LLM 失敗| answer_generator
    sql_validator -->|通過| sql_executor
    sql_validator -->|驗證失敗| repair_check{"可修正且 retry_count 小於 3？"}
    sql_executor -->|成功，含空結果| answer_generator
    sql_executor -->|執行失敗| repair_check
    repair_check -->|是| sql_repair
    repair_check -->|否| answer_generator
    sql_repair -->|取得修正版 SQL| sql_validator
    sql_repair -->|LLM 失敗| answer_generator
    answer_generator --> finish([END])
```

### Request Guard 資料異動攔截

目的：避免模型把「新增一位學生」「把成績改成 100」「刪除所有資料」等異動要求誤解成一般 SELECT，回傳無關結果。Guard 使用本機規則與 SQL 語法樹檢查，不需要另一個模型判斷意圖。

#### 呼叫與處理位置

| 程式位置 | 實際行為 |
| --- | --- |
| [request_guard.py](request_guard.py) 的 `is_write_request()` | 正規化問題；優先辨識直接輸入的 SQL，再檢查自然語言的異動動詞、目標與語境，回傳布林值 |
| [nodes.py](nodes.py) 的 `schema_loader()` | 在注入 Schema 前先執行 Guard；命中時清空本次 Schema、SQL 與查詢結果，設定 `error_kind="request"`、`status="failed"` |
| [graph.py](graph.py) 的 `after_schema()` | 看到 `error_message` 就直接轉入 `answer_generator`，跳過生成、驗證、執行與修正 |
| [nodes.py](nodes.py) 的 `answer_generator()` | 對 `request` 類錯誤直接回傳固定訊息，不加上一般 SQL 失敗的修正說明 |

正常每題由 `initial_state()` 將 `retry_count` 設為 0；被 Guard 攔截後不會進入 `sql_repair`，所以仍為 0。介面顯示失敗與固定回覆，SQL 保持空字串、`query_result` 為 `None`，不沿用先前的 SQL 或結果。

固定回覆來自 `WRITE_REQUEST_MESSAGE`：

> 已阻擋資料異動要求：此查詢器僅支援唯讀查詢，不能新增、刪除、修改資料或變更資料表結構。本次未執行 SQL。

「未執行 SQL」指被攔截的這次提問不會執行業務查詢；應用啟動時既有的資料庫權限檢查與 Schema 讀取仍會正常執行。

#### 比對規則與案例

問題先做 Unicode NFKC 正規化、移除 `Cf` 格式字元（例如零寬字元），並合併中文字之間的空白；英文比對不分大小寫。接著依序進行下列檢查：

1. **直接輸入的 SQL：** 略過開頭註解，辨識 `INSERT`、`UPDATE`、`DELETE`、`CREATE`、`ALTER`、`DROP`、`TRUNCATE`、`MERGE`、`COPY` 等異動指令；`GRANT`、`REVOKE`、`COMMENT` 等權限／中繼資料異動也會阻擋。
2. **`SELECT`／`WITH` 內的寫入：** 嘗試用 PostgreSQL 方言解析所有語句與子節點，攔截多語句中的寫入、資料異動 CTE，以及會建立資料表的 `SELECT INTO`。成功解析的純查詢不會因字串值或欄位名稱含 `UPDATE` 等文字而被當成異動。
3. **自然語言要求：** 檢查新增、插入、寫入、匯入、刪除、清空、修改、更新、改名、改成、設為等動作，以及「把成績加上 10」這類指定對象的數值異動。建立、增加、加入、設定及英文 `add／create／change／set` 等較模糊動詞，會搭配資料／表／欄位等目標、明確命令前綴或分句開頭判斷。

| 類別 | 會攔截的例子 |
| --- | --- |
| 新增資料 | 請新增一位學生到 students、插入一筆老師資料、`INSERT INTO ...` |
| 刪除資料 | 請刪除 students 的所有資料、清空 students、`DELETE FROM ...`、`TRUNCATE TABLE ...` |
| 修改資料 | 把學生的成績改成 100、更新授課科目、將分數設為 90、`UPDATE ... SET ...` |
| 異動資料表結構 | 建立資料表、新增欄位、重新命名欄位、`CREATE TABLE`、`ALTER TABLE`、`DROP TABLE` |
| 夾帶寫入的查詢 | `SELECT 1; UPDATE ...`、`WITH changed AS (UPDATE ... RETURNING *) SELECT ...`、`SELECT ... INTO ...` |

自然語言規則也會檢查相鄰分句、動作詞前的「已」「未」「不要」「請勿」或 `not`，以及查詢動詞、時間描述和「更新日期／修改紀錄」等語境。這能讓部分已發生事件、否定寫入及單純計算的問題繼續查詢；「先查詢再更新」等混合要求仍會整個阻擋。

| 輸入問題 | Guard 結果 |
| --- | --- |
| 請刪除 air_quality_hourly 的所有資料 | 直接阻擋，模型與 SQL 執行器都不呼叫 |
| 請新增一位學生到 students | 直接阻擋 |
| 把 students 裡張小明的成績改成 100 | 直接阻擋 |
| 請建立一張新的教師資料表 | 直接阻擋 |
| 先列出學生，再更新成績 | 整個要求直接阻擋，不先執行前半段 |
| 請統計今天新增的學生人數 | 通過 Guard，查詢已存在的資料 |
| 請列出資料的更新時間 | 通過 Guard，將「更新時間」視為查詢描述 |
| 請統計已刪除的學生資料有幾筆 | 通過 Guard，後續仍須依實際欄位產生與驗證 SQL |
| 不用修改，請統計學生人數 | 通過 Guard，繼續唯讀查詢流程 |
| 請計算學生資料的成績加上 10 的結果 | 通過 Guard，視為計算結果，後續仍只允許 SELECT |
| `SELECT 'UPDATE students SET score=100' AS example` | 通過 Guard；SQL 字串值中的指令文字不會被誤當成寫入 |

這仍是保守規則，不能完整理解所有自然語言，也不是通用的 SQL 安全沙箱。特殊改寫可能漏判，描述性文字也可能被誤擋；成功通過 Guard 不代表 SQL 或查詢語意已正確。模型產生的 SQL 仍須通過原本的 AST 驗證及 PostgreSQL 唯讀限制。調整動作詞、語境例外或固定回覆時，請一併更新對應測試。

### 六個節點

| 節點 | 職責 |
| --- | --- |
| `schema_loader` | 先攔截新增、刪除、修改及結構異動要求，再檢查問題非空並注入快取 Schema；不會逐次重讀資料庫 |
| `sql_generator` | 以固定規則、問題及 Schema 呼叫模型，要求只輸出一條純 SQL |
| `sql_validator` | 驗證、正規化 SQL，補上表 Schema 與最外層筆數限制 |
| `sql_executor` | 執行通過驗證的 SQL，取得欄位、資料列及截斷標記 |
| `sql_repair` | 附上失敗 SQL 與錯誤，再次呼叫模型，並增加修正次數 |
| `answer_generator` | 不呼叫模型；以 Python 產生結果或錯誤文字，設定最終狀態 |

### 修正與停止條件

| 錯誤類別 | 處理方式 |
| --- | --- |
| `request` | Guard 攔截的資料或資料表異動要求；直接顯示固定回覆，不呼叫模型、不執行 SQL、不進入修正 |
| `validation` | AST、白名單或支援範圍未通過，可進入修正 |
| `query` | SQLSTATE `42*`／`22*` 的語法或資料錯誤，以及 `57014` 查詢取消／逾時，可進入修正 |
| `connection` | 連線、驗證或部分服務／資源異常，直接失敗 |
| `permission` | 權限不足，直接失敗 |
| `database` | 其他未歸入可修正類別的資料庫錯誤，直接失敗 |
| `initialization`／`llm` | 空問題、Schema 無法使用或模型呼叫失敗，直接失敗 |

`retry_count` 從 0 開始，每次進入修正增加 1，修正時 LLM 失敗也會計數。**初次生成加三次修正，最多四次模型生成嘗試**；每一版都須重新驗證，未通過的 SQL 不會送到資料庫。第三次修正仍可成功；仍有錯就停止。

例如：模型生成不存在的欄位 → 資料庫回報 `42703` → 模型依 Schema 修正 → 再驗證 → 再執行 → 顯示結果。模型不保證能修好，也不保證查詢語意符合原問題。

Graph 的額外遞迴上限為 `GRAPH_RECURSION_LIMIT = 32`，不取代 `MAX_REPAIRS = 3` 的停止條件。

### 狀態欄位

| 欄位 | 內容 |
| --- | --- |
| `question`／`schema` | 去除頭尾空白的問題、本次使用的快取 Schema |
| `sql_query` | 最近一次生成或通過驗證的 SQL |
| `query_result` | `None`，或含 `columns`、`rows`、`truncated` 的結果 |
| `error_message`／`error_kind` | 最近的錯誤訊息及分類；成功的後續節點會清除錯誤 |
| `retry_count` | 本次查詢已嘗試修正的次數 |
| `final_answer` | 最終結果或失敗說明 |
| `status` | `running`、`repairing`、`success` 或 `failed` |

節點回傳局部狀態更新；未設定 Checkpointer，也沒有跨問題記憶。

## 資料庫執行與結果呈現

### 執行保護

- 建立連線時使用 5 秒 `connect_timeout` 與連線池 `pool_pre_ping`。
- Schema 讀取和查詢都在 `SET TRANSACTION READ ONLY` 的交易中進行。
- 交易內設定 `statement_timeout`、`lock_timeout = min(STATEMENT_TIMEOUT_MS, 1000)` 毫秒，以及 `search_path = pg_catalog`，不留下跨交易的設定。
- 查詢透過驅動串流讀取，最多抓取 `MAX_RESULT_ROWS + 1` 筆，再只保留顯示上限的資料。
- LLM 例外與資料庫例外會轉成固定訊息或整理後的 SQLSTATE 診斷，不直接呈現原始 provider 例外、連線 URL 或 SQLAlchemy traceback。

### 筆數與截斷

預設顯示上限是 100，SQL 驗證器會將**最外層**缺少或超過 101 的 LIMIT 設為 101；執行器取第 101 筆判斷是否需顯示截斷提示。更小的 LIMIT（包含 0）會保留。

這不會預先限制聚合的來源資料，`COUNT(*)`、AVG 等仍對查詢指定範圍計算。使用者明確指定 `LIMIT 5` 時，只取得這 5 筆，不會額外探測符合條件的總筆數。沒有 `ORDER BY` 時，前幾筆不保證順序。

### 顯示格式

結果是純文字欄位／值清單，例如：

```text
查詢結果（2 筆）：
月份：2025-01-01T00:00:00；平均值：100.0100
月份：2025-02-01T00:00:00；平均值：NULL
```

日期與時間使用 ISO 格式，Decimal 保留字串表示，空值顯示 `NULL`；不自動換算單位或四捨五入。資料列為空時回報「查無符合條件的資料」，仍視為成功；`COUNT(*)` 回傳 0 則正常顯示數值 0。超出上限會附上「結果已截斷」提示。

## 專案結構

```text
sql_query_agent/
├── .gitignore                 # 排除本機設定、虛擬環境與快取
└── text2sql/
    ├── app.py                 # 應用啟動、Gradio 介面與查詢事件
    ├── config.py              # 環境設定驗證與 ChatOllama 初始化
    ├── database.py            # Schema、權限檢查與唯讀 SQL 執行
    ├── graph.py               # LangGraph 狀態、節點連線與條件路由
    ├── nodes.py               # 六個節點、SQL 驗證與結果格式化
    ├── request_guard.py       # 模型呼叫前的資料與資料表異動攔截
    ├── ui.py                  # 主題與介面 HTML 元件
    ├── ui.css                 # 響應式版面及深淺色樣式
    ├── ui.js                  # 資料表名稱複製與狀態提示
    ├── requirements.txt       # 固定版本的直接相依套件
    ├── .env.example           # 可分享的環境變數範本
    ├── README.md              # 功能、設定、操作與開發說明
    └── tests/
        └── test_workflow.py   # 流程、驗證器、設定與介面離線測試
```

| 檔案 | 主要內容與維護位置 |
| --- | --- |
| [app.py](app.py) | `create_app()` 初始化設定、資料庫、Schema 與 Graph；組裝 Gradio、範例、送出與表名搜尋 callback；主程式啟動本機服務 |
| [config.py](config.py) | 不可變 `Settings`；`load_settings()` 載入及驗證環境變數；`create_llm()` 設定 ChatOllama、Bearer header、逾時及 `temperature=0` |
| [database.py](database.py) | `QueryResult`／`DatabaseError`；建立引擎、唯讀交易、權限與 Schema 檢查、SQL 執行、錯誤分類 |
| [graph.py](graph.py) | `AgentState`、`initial_state()`、`build_graph()`、六節點路由及修正／遞迴上限 |
| [nodes.py](nodes.py) | `SQL_RULES` 提示詞、AST／函式／型別白名單、`validate_sql()`、`format_answer()` 及 `make_nodes()` |
| [request_guard.py](request_guard.py) | `is_write_request()` 正規化問題、辨識 SQL 寫入節點與自然語言異動要求；`WRITE_REQUEST_MESSAGE` 提供固定的唯讀限制回覆 |
| [ui.py](ui.py) | `APP_THEME`、CSS／JS 檔案載入；頁首、概覽、區塊標題與可搜尋的資料表清單 HTML；動態表名會 HTML escape |
| [ui.css](ui.css) | 工作台版面、字級、色彩、卡片、捲動區、焦點樣式、深色及響應式設定 |
| [ui.js](ui.js) | 表名複製、相容備援、成功／失敗提示；事件委派讓搜尋更新後的表名按鈕仍可使用 |
| [requirements.txt](requirements.txt) | LangGraph、LangChain、langchain-ollama、SQLAlchemy、psycopg、Gradio、python-dotenv、SQLGlot 與 pytest 的直接相依版本 |
| [.env.example](.env.example) | 可複製的設定範本，API Key 留空；AQI 白名單不是資料庫安裝腳本 |
| [tests/test_workflow.py](tests/test_workflow.py) | 真實 Graph／Validator 搭配 Mock LLM、HTTP transport、資料庫與 Gradio callback 的離線測試 |
| [根目錄 .gitignore](../.gitignore) | 排除虛擬環境、Python／pytest 快取及本機憑證設定；保留 `.env.example` |

上方列出版本控制內的原始碼與設定範本；本機 `.env`、虛擬環境、快取及介面截圖未列入。實際資料表、資料內容及筆數由連線的 PostgreSQL 資料庫提供。

### 從 Python 使用

在 `text2sql` 目錄執行，下例會實際連線資料庫與模型：

```python
from config import create_llm, load_settings
from database import create_db_engine, execute_sql, load_schema
from graph import GRAPH_RECURSION_LIMIT, build_graph, initial_state

settings = load_settings()
engine = create_db_engine(settings)
try:
    schema = load_schema(engine, settings)
    workflow = build_graph(
        create_llm(settings), schema, settings.allowed_tables,
        lambda sql: execute_sql(engine, sql, settings),
        settings.max_result_rows,
    )
    question = f"請顯示 {settings.allowed_tables[0]} 的前 5 筆資料。"
    state = workflow.invoke(
        initial_state(question),
        config={"recursion_limit": GRAPH_RECURSION_LIMIT},
    )
    print(state["final_answer"])
finally:
    engine.dispose()
```

若需觀察各節點更新，可將 `invoke(...)` 換成 `stream(..., stream_mode="updates")` 並逐項讀取；更新內容可能包含 Schema、SQL 與查詢資料。`execute_sql()` 假設輸入已經通過驗證，不會自行呼叫 Validator，整合時應沿用 Graph 路徑。

若從其他 Python 程式啟動介面，須一併傳入主題及 CSS：

```python
from app import create_app
from ui import APP_CSS, APP_THEME

create_app().launch(
    server_name="127.0.0.1", share=False, show_error=False,
    css=APP_CSS, theme=APP_THEME, footer_links=[],
)
```

修改 `ui.py`、`ui.css` 或 `ui.js` 後，重新啟動即可載入。`app.py` 會預設停用 Gradio analytics，且不另建獨立 API 服務；Gradio 自身仍有提供介面事件的 Web 端點。

## 測試與驗證

在 `text2sql` 執行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

測試不需要真實 `.env`、API Key、網路、Ollama 或 PostgreSQL，涵蓋：

| 範圍 | 代表情境 |
| --- | --- |
| 工作流程 | 成功、異動要求直接攔截且不呼叫模型／執行器、空問題、空結果、每題新狀態、初始化失敗 |
| Request Guard | 繁簡中文與英文新增／刪除／修改／結構異動、全形與零寬字元、多語句、異動 CTE、SELECT INTO、SQL 字串值、混合要求、描述性查詢、否定／歷史語境，以及清除舊 SQL／結果 |
| 自動修正 | 驗證及執行錯誤修正、恰好三次停止、第三次修正成功、修正時 LLM 失敗 |
| SQL 驗證 | 非允許語法、資料來源與函式拒絕；CTE 作用域、表名補全、引號與結果 LIMIT |
| 資料庫 | 唯讀／逾時設定送出、Schema 讀取、角色與表權限拒絕、串流抓取上限及錯誤分類 |
| 模型設定 | 雲端 HTTPS／Key、網址與逾時檢查、本機無 Key、模型切換及敏感資訊遮蔽 |
| 原生 HTTP 回應 | Mock transport 驗證 `/api/chat`、Bearer、HTTP timeout、最終 SQL 與獨立 thinking 欄位分離、401 失敗不執行 SQL |
| Gradio | Mock 初始化後的送出 callback、SQL 與結果回傳、設定失敗可見 |

只看 Request Guard 相關測試：

```powershell
.\.venv\Scripts\python.exe -m pytest -v tests/test_workflow.py -k "write_request or readonly_questions"
```

這組測試使用真實 Graph 與 Mock LLM／執行器，確認被攔截時兩者皆未呼叫、修正次數為 0、SQL 與結果清空；同時確認一般問題、JOIN、描述性查詢與否定寫入仍可繼續。所有寫入語句都是測試輸入，不會真的新增、刪除或修改資料。

只看修正流程可使用：

```powershell
.\.venv\Scripts\python.exe -m pytest -v tests/test_workflow.py -k "syntax_error_repaired or database_sql_error_repaired or third_repair_can_succeed or stops_after_exactly_three_repairs"
```

**最近完整離線測試（2026-10-22）：** Windows／Python 3.12.7，`199 passed`，包含 93 個 Request Guard 相關案例。先前相依檢查（2026-10-21）的 `pip check` 無相依衝突。

這些離線測試不能證明真實帳號已通過權限檢查，也不能取代 PostgreSQL 唯讀阻擋、逾時、驅動串流或雲端模型的實測。本次文件更新未呼叫真實模型或執行實際資料庫查詢；部署前應另外確認連線、一般查詢、空結果、截斷及權限限制。

## 常見問題

| 現象 | 原因與處理方式 |
| --- | --- |
| 顯示「已阻擋資料異動要求」，SQL 為空且修正 0 次 | Request Guard 在模型生成前攔截新增、刪除、修改或結構異動；本次提問未執行 SQL。請改成符合唯讀用途的查詢問題 |
| 缺少環境變數 | 檢查 `text2sql/.env` 與必填值；留意系統環境變數優先，不會被 `.env` 蓋掉 |
| `DATABASE_URL` 格式不符 | 使用 `postgresql+psycopg://`；帳密中的保留字元須 URL encode |
| 白名單設定被拒絕 | 使用小寫 `schema.table`，移除系統 Schema、特殊字元及空項目 |
| 帳號權限過大 | 檢查管理旗標、資料庫 CREATE／TEMP、角色成員資格及 `PUBLIC` 授權 |
| 資料表權限不符合唯讀要求 | 檢查白名單表是否可 SELECT、是否仍有寫入類權限，以及 Schema 是否可 CREATE |
| 找不到白名單資料表 | 確認連到正確資料庫與 Schema、表確實存在且為一般表／分割表；應用不自動建表 |
| Ollama 呼叫失敗 | 檢查服務、模型完整名稱、Key、額度及逾時；連線驗證錯誤不會觸發 SQL 修正 |
| SQL 驗證持續失敗 | 查詢可能超出 AST／函式支援範圍，或模型持續輸出 Markdown；縮小問題並審閱 SQL |
| SQL 查詢逾時 | 縮小日期或 JOIN 範圍、檢查索引與查詢計畫；逾時可進入修正，但不保證修正成功 |
| 結果已截斷 | 縮小查詢範圍、明確排序，或在允許範圍內調整 `MAX_RESULT_ROWS` 後重啟 |
| 側欄找不到新表／Schema 過時 | 同步處理授權、白名單與重啟；側欄搜尋只篩選已設定的名稱 |
| 表名無法複製 | 依介面提示手動選取複製，或檢查瀏覽器剪貼簿權限 |
| 介面仍顯示待完成設定 | 依啟動錯誤修正後重新啟動；運行中的應用不會自動重建失敗的初始化流程 |

遇到「帳號權限過大」時，可用應用登入帳號檢查目前資料庫的有效權限：

```sql
SELECT
    has_database_privilege(current_database(), 'CREATE') AS can_create,
    has_database_privilege(current_database(), 'TEMP') AS can_create_temp;
```

兩者都應為 false。若 TEMP 來自 `PUBLIC`，由管理員依專用／共用資料庫的使用情況調整，不能只撤銷登入角色的直接授權。

## 已知限制

- **Request Guard 範圍：** 結合 SQL 語法檢查與自然語言規則，攔截明確新增、刪除、修改及結構異動，不是通用的意圖分類或完整提示注入防護；特殊改寫可能漏判，描述性文字也可能被誤擋。模型輸出的 SQL 驗證及 PostgreSQL 唯讀交易與權限仍不可省略。
- **用途與安全邊界：** 適用受控資料庫的本機 MVP。SQL Parser 不是安全沙箱；唯讀交易也不能阻止所有函式或外部資料來源的副作用。資料庫、物件建立者、型別、運算子、政策及擴充套件須可信任。
- **權限檢查範圍：** 啟動檢查不是完整 PostgreSQL 權限稽核，未完整盤點所有角色、其他 Schema、欄位層級授權、自訂函式與擴充能力。帳號不應擁有資料庫物件或伺服器檔案／程式執行能力。
- **查詢正確性：** SQL 能執行不代表問題理解正確；應審閱日期、時區、JOIN、分組與 NULL 語意。沒有業務字典，亦不自動推知單位、時區或主外鍵關係。
- **資源上限：** 結果筆數限制不限制每個欄位大小或資料庫掃描量；聚合與 JOIN 仍可能昂貴。多次修正會累積模型及資料庫等待，沒有整個 Graph 的總時間上限。
- **介面功能：** 結果為文字，沒有可排序的結果表格、CSV 匯出、圖表、登入／授權介面或背景工作管理。預設本機綁定與無分享不等同於完整存取控制。
- **模型與記憶：** 只支援單一 Ollama 設定，沒有多模型切換策略、工具呼叫、Web Search、RAG 或長期對話記憶。
