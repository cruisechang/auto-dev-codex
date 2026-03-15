# autoresearch-codex (for software development)

這個版本把 `autoresearch` 的概念改成「程式開發迭代器」：

1. 代理自主分析目前程式與評量結果。
2. 代理自主修改程式碼。
3. 系統跑 evaluator 產生分數。
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

## 安全機制

- 只允許修改 `editable_paths` 內檔案。
- 修改超出範圍會自動回退。
- 評量失敗或解析失敗會標記為 `crash` 並回退該輪。
- 所有結果寫入 `results.tsv`；每輪 log 在 `.autodev/runs/`。

## 建議調整

- 把 evaluator 換成你的真實 KPI（測試通過率、benchmark、錯誤率、靜態檢查）。
- 在 `program.md` 明確寫出你希望代理優先優化的項目。
- 先用小 `max_iterations` 驗證流程，再放大迴圈。
