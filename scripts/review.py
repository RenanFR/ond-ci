import json
import os
import re
import sys

import requests
from anthropic import Anthropic

MODEL = "claude-sonnet-5"
MAX_DIFF_CHARS = 80000
MAX_CONVENTIONS_CHARS = 20000

GITHUB_API = "https://api.github.com"


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
Revise o diff abaixo de um Pull Request e responda EXCLUSIVAMENTE com um JSON válido,
sem markdown ao redor, no formato:

{{
  "verdict": "APPROVE" ou "REQUEST_CHANGES",
  "summary": "resumo objetivo do parecer, em português, 2-4 frases",
  "findings": [
    {{"file": "caminho/do/arquivo", "line": 123, "severity": "blocking" ou "nit", "description": "..."}}
  ]
}}

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
    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    raw_text = "".join(block.text for block in message.content if block.type == "text")
    match = re.search(r"\{.*\}", raw_text, re.DOTALL)
    if not match:
        raise ValueError(f"Resposta do Claude sem JSON reconhecível: {raw_text}")
    return json.loads(match.group(0))


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


def main():
    event = load_event()
    pull_request = event["pull_request"]
    repo_full_name = event["repository"]["full_name"]
    pull_number = pull_request["number"]
    token = os.environ["GITHUB_TOKEN"]

    diff = fetch_diff(repo_full_name, pull_number, token)
    if not diff.strip():
        print("Diff vazio, nada para revisar.")
        return

    conventions = load_conventions()
    prompt = build_prompt(pull_request, diff, conventions)
    review = call_claude(prompt)

    verdict = review.get("verdict")
    if verdict not in ("APPROVE", "REQUEST_CHANGES"):
        print(f"Veredito inesperado do Claude: {verdict!r}, abortando sem postar review.")
        sys.exit(1)

    body = format_review_body(review)
    submit_review(repo_full_name, pull_number, token, verdict, body)
    print(f"Review postado: {verdict}")


if __name__ == "__main__":
    main()
