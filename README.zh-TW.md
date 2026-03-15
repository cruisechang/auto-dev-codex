# autodev codex 版

這個版本把 `autoresearch` 的概念改成「程式開發迭代器」：

1. 代理自主分析目前程式與評量結果。
2. 代理自主修改程式碼。
3. 系統執行 evaluator 產生分數。
4. 分數有進步才保留（commit），否則自動回退。
5. 重複迭代，讓指標持續往更好方向走。

## 檔案說明

- `autodev.py`: 主迴圈（agent edit -> evaluate -> keep/discard）
- `autodev.toml.example`: 設定範本
- `program.md`: 代理行為規範
- `tools/evaluate_pytest.py`: 範例評量器（輸出 JSON score）

## 快速開始

1. 進入你要優化的 git 專案。
2. 複製這些檔案到該專案。
3. 建立設定檔：

```bash
cp autodev.toml.example autodev.toml
```

4. 修改 `autodev.toml` 的兩個重點：

- `[agent].command`: 換成你的 coding agent 指令（會依 `program.md`/prompt 修改程式）
- `[workspace].editable_paths`: 限制可改檔案範圍

5. 執行：

```bash
python3 autodev.py --config autodev.toml
```

## Evaluator 介面

`[evaluator].command` 必須輸出 JSON，至少包含：

```json
{"score": 83.2, "summary": "..."}
```

- `score` 會被用來決定 keep/discard。
- `higher_is_better = true` 代表分數越高越好；反之越低越好。

## 預設固定指標（tools/evaluate_pytest.py）

目前範例 evaluator 固定輸出 4 個指標：

- `Test Pass Rate`：測試通過率（%）
- `Coverage`：程式覆蓋率（%）
- `Lint Errors`：lint 錯誤數
- `Type Errors`：型別檢查錯誤數

預設計分公式：

```text
score = Test Pass Rate * 0.5 + Coverage * 0.3 - Lint Errors * 2 - Type Errors * 2
```

範例 evaluator 會嘗試執行：

- `python3 -m pytest -q`
- `python3 -m pytest --cov=. --cov-report=term -q`
- `python3 -m ruff check .`
- `python3 -m mypy .`

如果某工具執行失敗，會在 `summary` 的 `notes` 記錄原因，並使用保守 fallback 值。

### 執行時選擇要用的指標

可在執行時決定只用其中 1 個或 2 個指標計分。

直接執行 evaluator：

```bash
python3 tools/evaluate_pytest.py --use-metrics test_pass_rate
python3 tools/evaluate_pytest.py --use-metrics test_pass_rate,coverage
python3 tools/evaluate_pytest.py --use-metrics lint_errors,type_errors
```

跑 autodev 時（不改 `autodev.toml`）：

```bash
AUTODEV_METRICS=test_pass_rate,coverage python3 autodev.py --config autodev.toml
```

可用鍵值：

- `test_pass_rate`
- `coverage`
- `lint_errors`
- `type_errors`

## 如何新增指標

1. 在 `tools/evaluate_pytest.py` 新增指標計算邏輯。
2. 在輸出 JSON 的 `metrics` 與 `fixed_metrics` 加入新欄位。
3. 把新指標納入 `score` 計算公式（設定權重或懲罰係數）。
4. 更新 `summary` 文字，讓每輪 log 看得到新指標。

## 如何修改指標

1. 修改指標來源指令（例如把 `ruff` 換成 `eslint`）。
2. 修改解析函式（例如錯誤行數的 regex 規則）。
3. 修改 `score` 權重，調整優化方向。
4. 若要改主分數欄位名稱，記得同步修改 `autodev.toml` 的 `[evaluator].score_key`。

## 安全機制

- 只允許修改 `editable_paths` 內檔案。
- 修改超出範圍會自動回退。
- 評量失敗或解析失敗會標記為 `crash` 並回退該輪。
- 所有結果寫入 `results.tsv`；每輪 log 在 `.autodev/runs/`。

## 建議調整

- 把 evaluator 換成你的真實 KPI（測試通過率、benchmark、錯誤率、靜態檢查）。
- 在 `program.md` 明確寫出你希望代理優先優化的項目。
- 先用小 `max_iterations` 驗證流程，再放大迴圈。
