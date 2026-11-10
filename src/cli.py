import sys
from pathlib import Path
from typing import Optional
from datetime import datetime

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.podcast_cutter import AutoPodcastCutter
from src.google_services import get_credentials, YouTubeService, run_oauth_login

app = typer.Typer(
    help="AutoShorts Pipeline: Mineração com IA/Heurística, Auditoria QA, Edição 9:16 Vertical com Legendas Dinâmicas e Publicação Automática no YouTube."
)
console = Console(force_terminal=True, legacy_windows=False)


@app.command()
def clip(
    url: str = typer.Argument(..., help="Link do YouTube (podcast, entrevista ou vídeo longo)"),
    upload: bool = typer.Option(True, "--upload/--no-upload", help="Fazer upload automático no canal do YouTube"),
    privacy: str = typer.Option("private", "--privacy", "-p", help="Visibilidade no YouTube (private, unlisted, public)"),
    publish_at: Optional[str] = typer.Option(None, "--publish-at", help="Data de agendamento ISO UTC (ex: 2026-10-18T18:00:00Z)"),
    count: int = typer.Option(1, "--count", "-n", help="Número de cortes virais a gerar (padrão: 1)"),
    start_sec: Optional[float] = typer.Option(None, "--start", "-s", help="Segundo inicial do corte (ex: 2348.4)"),
    end_sec: Optional[float] = typer.Option(None, "--end", "-e", help="Segundo final do corte (ex: 2385.1)"),
    title: Optional[str] = typer.Option(None, "--title", "-t", help="Título personalizado para o Short"),
    hook: Optional[str] = typer.Option(None, "--hook", "-k", help="Texto do banner superior de gancho (ex: '🚨 O MAIOR ERRO DA VIDA')"),
    music: Optional[str] = typer.Option(None, "--music", "-m", help="Caminho do arquivo de música de fundo .mp3"),
):
    """Corta automaticamente um podcast do YouTube em Shorts 9:16 verticais e publica no canal."""
    parsed_publish_at = None
    if publish_at:
        try:
            clean_iso = publish_at.replace("Z", "+00:00")
            parsed_publish_at = datetime.fromisoformat(clean_iso)
            console.print(f"[cyan]Programando publicação no YouTube para:[/] {parsed_publish_at}")
        except Exception as e:
            console.print(f"[yellow]Aviso: Data inválida ({e}). Publicando sem agendamento.[/]")

    console.print(
        Panel.fit(
            f"[bold magenta]🎬 AutoShorts Pipeline Iniciado[/]\n"
            f"[bold white]URL:[/] {url}\n"
            f"[bold white]Cortes solicitados:[/] {count}\n"
            f"[bold white]Upload no YouTube:[/] {'Sim' if upload else 'Não'}\n"
            f"[bold white]Visibilidade:[/] {privacy}\n"
            f"[bold white]Formato:[/] 1080x1920 (9:16 Vertical) + Legendas Dinâmicas + BGM + QA Guardrails",
            title="AutoShorts Podcast",
            border_style="magenta",
        )
    )

    cutter = AutoPodcastCutter()
    results = cutter.process_url(
        youtube_url=url,
        upload_to_youtube=upload,
        privacy=privacy,
        publish_at=parsed_publish_at,
        max_clips=count,
        custom_start=start_sec,
        custom_end=end_sec,
        custom_title=title,
        custom_hook=hook,
        music_path=music,
    )

    if not results:
        console.print("[bold red]Nenhum corte foi gerado.[/]")
        raise typer.Exit(code=1)

    for res in results:
        console.print(
            Panel(
                f"[bold green]✔ Corte {res['clip_number']} Concluído![/]\n\n"
                f"📌 [bold]Título:[/] {res['title']}\n"
                f"⏱ [bold]Duração:[/] {res['duration']:.1f}s\n"
                f"📂 [bold]Arquivo:[/] {res['file_path']}\n"
                + (f"🔗 [bold cyan]YouTube:[/] {res['youtube_link']}\n" if res.get('youtube_link') else "")
                + f"💡 [dim]{res.get('viral_reason', '')}[/]",
                border_style="green",
            )
        )


@app.command()
def auth():
    """Valida as credenciais da conta do YouTube conectada."""
    try:
        console.print("[dim]Verificando credenciais Google OAuth...[/]")
        creds = get_credentials()
        yt = YouTubeService(creds)
        ch_resp = yt.service.channels().list(mine=True, part="snippet,statistics").execute()
        items = ch_resp.get("items", [])
        if items:
            ch = items[0]
            title = ch["snippet"]["title"]
            subs = ch["statistics"].get("subscriberCount", "0")
            vids = ch["statistics"].get("videoCount", "0")
            console.print(
                Panel(
                    f"[bold green]✔ Conta Google Conectada com Sucesso![/]\n\n"
                    f"📺 [bold]Canal:[/] {title}\n"
                    f"👥 [bold]Inscritos:[/] {subs}\n"
                    f"🎬 [bold]Vídeos:[/] {vids}",
                    title="Status YouTube",
                    border_style="green",
                )
            )
        else:
            console.print("[yellow]Conectado, mas nenhum canal público ativo encontrado.[/]")
    except Exception as e:
        console.print(Panel(f"[bold red]Erro ao validar credenciais:[/] {e}", border_style="red"))
        raise typer.Exit(code=1)


@app.command()
def login():
    """Conecta (ou reconecta) uma conta Google/YouTube via OAuth."""
    try:
        console.print("[cyan]Abrindo o navegador para login no Google...[/]")
        creds = run_oauth_login()
        yt = YouTubeService(creds)
        ch_resp = yt.service.channels().list(mine=True, part="snippet").execute()
        items = ch_resp.get("items", [])
        if items:
            title = items[0]["snippet"]["title"]
            console.print(Panel(
                f"[bold green]✔ Conta conectada com sucesso![/]\n\n"
                f"📺 [bold]Canal:[/] {title}",
                title="Login YouTube",
                border_style="green",
            ))
        else:
            console.print("[green]✔ Login realizado. Nenhum canal ativo encontrado.[/]")
    except Exception as e:
        console.print(Panel(f"[bold red]Erro no login:[/] {e}", border_style="red"))
        raise typer.Exit(code=1)


@app.command()
def metrics(
    limit: int = typer.Option(20, "--limit", "-l", help="Número máximo de vídeos recentes a listar")
):
    """Exibe um painel completo de métricas em tempo real dos vídeos do canal conectado."""
    try:
        creds = get_credentials()
        yt = YouTubeService(creds).service
        ch_resp = yt.channels().list(mine=True, part="snippet,statistics,contentDetails").execute()
        items = ch_resp.get("items", [])
        if not items:
            console.print("[yellow]Nenhum canal encontrado na conta autenticada.[/]")
            raise typer.Exit(code=1)

        ch = items[0]
        ch_title = ch["snippet"]["title"]
        subs = ch["statistics"].get("subscriberCount", "0")
        uploads_pl = ch["contentDetails"]["relatedPlaylists"]["uploads"]

        pl_resp = yt.playlistItems().list(
            playlistId=uploads_pl, part="snippet,contentDetails", maxResults=min(50, limit)
        ).execute()
        vid_ids = [item["contentDetails"]["videoId"] for item in pl_resp.get("items", [])]

        if not vid_ids:
            console.print(f"[yellow]Canal '{ch_title}' ainda não possui vídeos enviados.[/]")
            return

        v_resp = yt.videos().list(id=",".join(vid_ids), part="snippet,statistics,status").execute()

        table = Table(title=f"📊 Métricas em Tempo Real — Canal: {ch_title} ({subs} inscritos)")
        table.add_column("#", justify="right", style="dim")
        table.add_column("Status", justify="center")
        table.add_column("Título", style="bold white")
        table.add_column("Views", justify="right", style="cyan")
        table.add_column("Likes", justify="right", style="green")
        table.add_column("Engaj. (%)", justify="right", style="magenta")
        table.add_column("Link", style="blue")

        total_views = 0
        total_likes = 0

        for idx, v in enumerate(v_resp.get("items", []), start=1):
            vid_id = v["id"]
            v_title = v["snippet"]["title"][:50]
            privacy = v["status"]["privacyStatus"]
            publish_at = v["status"].get("publishAt")
            status_str = "🟢 PÚBLICO" if privacy == "public" else ("⏳ AGENDADO" if publish_at else f"🔒 {privacy.upper()}")

            stats = v.get("statistics", {})
            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            total_views += views
            total_likes += likes
            eng = f"{(likes / views * 100):.2f}%" if views > 0 else "0.00%"

            table.add_row(
                str(idx),
                status_str,
                v_title,
                f"{views:,}".replace(",", "."),
                str(likes),
                eng,
                f"https://youtube.com/shorts/{vid_id}",
            )

        console.print(table)
        console.print(
            Panel.fit(
                f"🔥 [bold cyan]Visualizações Acumuladas nos Vídeos Listados:[/] {total_views:,}".replace(",", ".")
                + f"\n👍 [bold green]Curtidas Acumuladas:[/] {total_likes}",
                border_style="cyan",
            )
        )
    except Exception as e:
        console.print(Panel(f"[bold red]Erro ao consultar métricas:[/] {e}", border_style="red"))
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
