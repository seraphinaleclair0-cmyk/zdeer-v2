# Zdeer 达人合作系统 v2

## 你只需要操作 Google Sheets，其他全自动

---

## 日常使用流程

### 群发邀约
1. 打开 Google Sheets「待发名单」
2. 填入达人信息（名字、邮箱、TikTok链接）
3. 「发件邮箱」列选择用哪个邮箱发
4. 「发送」列填 ✅
5. 系统每天午夜自动发出

### 查看回复 & 回复达人
1. 每天早上打开「沟通管理」Sheet
2. 看 AI 填好的回复摘要和建议回复
3. 如果建议回复可以用：直接在「发送」列填 ✅
4. 如果想调整：在「你的指令」列写一句中文 → AI 重新生成 → 填 ✅

---

## Sheet 结构

### 「待发名单」（A~H列）
| 列 | 说明 | 谁来填 |
|----|------|-------|
| 达人名字 | 英文名 | 你 |
| 邮箱 | 达人邮箱 | 你 |
| TikTok链接 | @账号或链接 | 你 |
| 发件邮箱 | 选用哪个账号发 | 你 |
| 发送 | 填 ✅ 触发发送 | 你 |
| 状态 | 已发送/发送失败 | 自动 |
| 发送时间 | 实际发送时间 | 自动 |
| 跟进次数 | 已跟进几次 | 自动 |

### 「沟通管理」（A~J列）
| 列 | 说明 | 谁来填 |
|----|------|-------|
| 日期 | 回复日期 | 自动 |
| 达人名字 | 匹配名单 | 自动 |
| 邮箱 | 达人邮箱 | 自动 |
| 回复摘要 | AI提取 | 自动 |
| 过往沟通 | 历史记录 | 自动 |
| 当前阶段 | AI识别 | 自动 |
| 状态 | 待回复/已回复/已成交/已放弃 | 你来改 |
| 你的指令 | 一句中文说怎么回 | 你来填（可选）|
| AI生成回复 | 完整英文邮件 | 自动 |
| 发送 | 填 ✅ 立即发出 | 你 |

---

## 首次配置步骤

### 第一步：GitHub Secrets 配置
仓库 → Settings → Secrets and variables → Actions → New repository secret

| Secret名称 | 内容 |
|-----------|------|
| GMAIL_PASS_1 | seraphinaleclair0@gmail.com 应用专用密码 |
| GMAIL_PASS_2 | eloise87657@gmail.com 应用专用密码 |
| GMAIL_PASS_3 | cjtjieting@gmail.com 应用专用密码 |
| GMAIL_PASS_4 | pr@zdeer.org 应用专用密码 |
| GEMINI_API_KEY | 你的 Gemini API Key |
| GOOGLE_TOKEN_JSON | token.json 文件的完整内容 |

### 第二步：推送代码到 GitHub
```bash
cd zdeer-v2
git init
git remote add origin https://github.com/seraphinaleclair0-cmyk/zdeer-v2.git
git add .
git commit -m "init"
git push -u origin main
```

### 第三步：手动触发测试
GitHub → Actions → 「Zdeer 每日自动运行」→ Run workflow

---

## 修改邮件文案
编辑 template.py 里的 get_outreach_body()（初次邮件）和 get_followup_body()（跟进邮件）

## 修改筹码库
编辑 negotiation_cards.txt，按阶段填写你的谈判条件
