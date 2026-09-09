import json
import os
import sys

import requests
from anthropic import Anthropic

MODEL = "claude-sonnet-5"
MAX_DIFF_CHARS = 300000
MAX_CONVENTIONS_CHARS = 20000
MAX_OUTPUT_TOKENS = 64000
REVIEW_ATTEMPTS = 2

GITHUB_API = "https://api.github.com"
CHECK_NAME = "ai-review"

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["APPROVE", "REQUEST_CHANGES"]},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "severity": {"type": "string", "enum": ["blocking", "nit"]},
                    "description": {"type": "string"},
                },
                "required": ["file", "line", "severity", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdict", "summary", "findings"],
    "additionalProperties": False,
}


def load_event():
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as event_file:
        return json.load(event_file)


def github_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def fetch_diff(repo_full_name, pull_number, token):
    url = f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pull_number}"
    headers = github_headers(token)
    headers["Accept"] = "application/vnd.github.v3.diff"
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()
    diff = response.text
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n[diff truncado por tamanho]"
    return diff


def load_conventions():
    if not os.path.exists("CLAUDE.md"):
        return ""
    with open("CLAUDE.md", encoding="utf-8") as conventions_file:
        content = conventions_file.read()
    if len(content) > MAX_CONVENTIONS_CHARS:
        content = content[:MAX_CONVENTIONS_CHARS] + "\n\n[CLAUDE.md truncado por tamanho]"
    return content


def build_prompt(pull_request, diff, conventions):
    title = pull_request.get("title", "")
    body = pull_request.get("body") or ""
    conventions_block = (
        f"Convenções do projeto (CLAUDE.md), aplique-as como critério de bloqueio:\n\n{conventions}\n"
        if conventions
        else "Nenhum CLAUDE.md encontrado no repositório.\n"
    )
    return f"""Você é um revisor de código rigoroso para o ecossistema OND (Agamatec).
Revise o diff abaixo de um Pull Request. O resumo do parecer vai em "summary", em
português, em 2 a 4 frases, e cada achado vira um item de "findings" com o caminho do
arquivo, a linha e a severidade e a descrição. Use 0 na linha quando o achado não estiver
preso a uma linha específica.

Marque "verdict": "REQUEST_CHANGES" se houver QUALQUER achado com severity "blocking":
- bug de corretude, condição de corrida, null/erro não tratado, regressão de comportamento
- falha de segurança (injeção, exposição de segredo, validação ausente em fronteira de confiança)
- violação explícita de uma convenção do CLAUDE.md abaixo

Achados de estilo, nomenclatura discutível ou sugestões de simplificação que não violem o
CLAUDE.md são "nit" e não bloqueiam aprovação. Se não houver nenhum achado "blocking",
"verdict" deve ser "APPROVE". Não invente achados; se o diff estiver correto, aprove.

{conventions_block}
Título do PR: {title}
Descrição do PR: {body}

Diff:
```diff
{diff}
```"""


def call_claude(prompt):
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    last_failure = "nenhuma tentativa executada"
    for attempt in range(1, REVIEW_ATTEMPTS + 1):
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": prompt}],
            output_config={"format": {"type": "json_schema", "schema": REVIEW_SCHEMA}},
        ) as stream:
            message = stream.get_final_message()

        raw_text = "".join(block.text for block in message.content if block.type == "text")
        if not raw_text.strip():
            block_types = [block.type for block in message.content]
            last_failure = (
                f"tentativa {attempt} terminou sem parecer escrito "
                f"(stop_reason={message.stop_reason!r}, blocos recebidos={block_types!r})"
            )
            print(f"Revisão sem parecer utilizável, repetindo. {last_failure}", file=sys.stderr)
            continue
        return json.loads(raw_text)

    raise ValueError(f"O Claude não devolveu um parecer utilizável. Última falha: {last_failure}")


def format_review_body(review):
    lines = [review["summary"], ""]
    findings = review.get("findings") or []
    blocking = [finding for finding in findings if finding.get("severity") == "blocking"]
    nits = [finding for finding in findings if finding.get("severity") != "blocking"]

    if blocking:
        lines.append("**Bloqueios:**")
        for finding in blocking:
            location = f"`{finding.get('file', '?')}`"
            if finding.get("line"):
                location += f":{finding['line']}"
            lines.append(f"- {location} — {finding.get('description', '')}")
        lines.append("")

    if nits:
        lines.append("**Sugestões (não bloqueiam):**")
        for finding in nits:
            location = f"`{finding.get('file', '?')}`"
            if finding.get("line"):
                location += f":{finding['line']}"
            lines.append(f"- {location} — {finding.get('description', '')}")
        lines.append("")

    lines.append("_Revisão automática por Claude (ond-ai-reviewer)._")
    return "\n".join(lines)


def submit_review(repo_full_name, pull_number, token, event, body):
    url = f"{GITHUB_API}/repos/{repo_full_name}/pulls/{pull_number}/reviews"
    response = requests.post(
        url,
        headers=github_headers(token),
        json={"event": event, "body": body},
        timeout=30,
    )
    response.raise_for_status()


def submit_check_run(repo_full_name, head_sha, token, conclusion, title, body):
    url = f"{GITHUB_API}/repos/{repo_full_name}/check-runs"
    payload = {
        "name": CHECK_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": conclusion,
        "output": {"title": title, "summary": body},
    }
    response = requests.post(url, headers=github_headers(token), json=payload, timeout=30)
    response.raise_for_status()


def main():
    event = load_event()
    pull_request = event["pull_request"]
    repo_full_name = event["repository"]["full_name"]
    pull_number = pull_request["number"]
    head_sha = pull_request["head"]["sha"]
    token = os.environ["GITHUB_TOKEN"]

    diff = fetch_diff(repo_full_name, pull_number, token)
    if not diff.strip():
        print("Diff vazio, nada para revisar.")
        return

    conventions = load_conventions()
    prompt = build_prompt(pull_request, diff, conventions)

    try:
        review = call_claude(prompt)
    except Exception as review_failure:
        reason = f"A revisão automática não foi concluída: {review_failure}"
        print(reason, file=sys.stderr)
        submit_check_run(repo_full_name, head_sha, token, "failure", "Revisão não concluída", reason)
        sys.exit(1)

    verdict = review["verdict"]
    body = format_review_body(review)
    conclusion = "success" if verdict == "APPROVE" else "failure"
    title = "Aprovado" if verdict == "APPROVE" else "Mudanças solicitadas"
    submit_review(repo_full_name, pull_number, token, verdict, body)
    submit_check_run(repo_full_name, head_sha, token, conclusion, title, body)
    print(f"Review e check postados: {verdict}")


if __name__ == "__main__":
    main()
