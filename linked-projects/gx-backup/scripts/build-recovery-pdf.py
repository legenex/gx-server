#!/usr/bin/env python3
"""Build docs/GX-BACKUP-RESTORE-GUIDE.pdf from the recovery markdown."""
from pathlib import Path
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    Preformatted,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "GX-BACKUP-RESTORE-GUIDE.pdf"


def p(text, style):
    return Paragraph(text.replace("\n", "<br/>"), style)


def main() -> None:
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle("Cover", parent=styles["Title"], fontSize=22, spaceAfter=12))
    styles.add(ParagraphStyle("H", parent=styles["Heading1"], fontSize=14, spaceBefore=14, textColor=colors.HexColor("#111")))
    styles.add(ParagraphStyle("Body", parent=styles["BodyText"], fontSize=10, leading=14, spaceAfter=8))
    styles.add(ParagraphStyle("CodeBlock", parent=styles["Code"], fontSize=8, leading=11, backColor=colors.HexColor("#f4f4f1"), spaceAfter=10))
    styles.add(ParagraphStyle("Warn", parent=styles["BodyText"], fontSize=11, leading=15, textColor=colors.HexColor("#7a1d1d"), spaceAfter=10))

    story = []
    story.append(p("GX Backup — Recovery and Reinstall Guide", styles["Cover"]))
    story.append(p("Dual ASUS GX10 cluster (gx10-01 + gx10-02). Generated 2026-09-23.", styles["Body"]))
    story.append(p("<b>EMERGENCY RECOVERY</b>", styles["H"]))
    story.append(p(
        "If a machine dies: (1) On the surviving node run <b>gx doctor</b>. "
        "(2) Install Ubuntu 24.04 on the replacement GX10. "
        "(3) Clone the private <b>legenex/gx-backup</b> repository to "
        "<font face='Courier'>/home/legenex/Documents/Projects/gx-backup</font>. "
        "(4) Copy <font face='Courier'>~/.config/gx-backup/restic-password</font> and "
        "<font face='Courier'>age.key</font> from the surviving node. "
        "(5) Run <font face='Courier'>./install</font> then <font face='Courier'>gx restore latest</font>. "
        "(6) Recreate model weights from <font face='Courier'>models.lock</font>. "
        "(7) Start compose + user systemd units. (8) <b>gx doctor</b>.",
        styles["Warn"],
    ))
    story.append(p("What GX Backup is", styles["H"]))
    story.append(p(
        "Git holds the rebuild recipe (scripts, pins, sanitized configs, this guide). "
        "Restic holds encrypted mutable data. Public model weights are not in Git and are not duplicated into every snapshot.",
        styles["Body"],
    ))
    story.append(p("Everyday commands", styles["H"]))
    story.append(Preformatted(
        "gx status\ngx backup\ngx doctor\ngx models\ngx auto\ngx mini\ngx code\ngx max\ngx logs\ngx benchmark",
        styles["CodeBlock"],
    ))
    rows = [
        [p("<b>What</b>", styles["Body"]), p("<b>Where</b>", styles["Body"])],
        [p("Recipe", styles["Body"]), p("GitHub legenex/gx-backup (private) and this checkout", styles["Body"])],
        [p("Encrypted data gx10-01", styles["Body"]), p("~/Documents/Backups/restic/local and peer copy on gx10-02", styles["Body"])],
        [p("Encrypted data gx10-02", styles["Body"]), p("~/Documents/Backups/restic/local on gx10-02 and peer copy on gx10-01", styles["Body"])],
        [p("Restic password", styles["Body"]), p("~/.config/gx-backup/restic-password on both nodes (0600). Never in Git.", styles["Body"])],
        [p("Models", styles["Body"]), p("/srv/models plus models.lock pins and sha256", styles["Body"])],
        [p("Gateway", styles["Body"]), p("http://127.0.0.1:4000/v1  models gx-auto gx-mini gx-code gx-max", styles["Body"])],
        [p("OpenWebUI", styles["Body"]), p("http://127.0.0.1:3000  volume open-webui", styles["Body"])],
        [p("GX-Cluster", styles["Body"]), p("http://127.0.0.1:8088  Backup page starts gx backup", styles["Body"])],
    ]
    tbl = Table(rows, colWidths=[1.8 * inch, 5.0 * inch])
    tbl.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ecece8")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(tbl)
    story.append(p("Restore one file / project / database", styles["H"]))
    story.append(Preformatted(
        "export RESTIC_PASSWORD_FILE=$HOME/.config/gx-backup/restic-password\n"
        "restic -r $HOME/Documents/Backups/restic/local snapshots\n"
        "restic -r $HOME/Documents/Backups/restic/local restore latest \\\n"
        "  --target /tmp/restore --include /path/to/file\n"
        "# SQL dumps: Documents/Backups/dumps/<stamp>/*.sql.gz\n"
        "# OpenWebUI: open-webui-data.tar.gz into the docker volume",
        styles["CodeBlock"],
    ))
    story.append(p("Rebuild from fresh Ubuntu", styles["H"]))
    story.append(p(
        "Same hardware class (ASUS GX10 / NVIDIA GB10). Create the matching user, install Docker CE, add the user to docker. "
        "Configure Tailscale and the two ConnectX-7 nets (192.168.100.10/11 and 192.168.101.10/11). "
        "SSH host gx10-02 uses key id_ed25519_gxcluster. Then follow Emergency Recovery above. "
        "Do not reboot both nodes at once.",
        styles["Body"],
    ))
    story.append(p("Nodes discover each other", styles["H"]))
    story.append(p(
        "SSH: ~/.ssh/config Host gx10-02 → 100.73.238.4 user legenex-02. "
        "Model traffic: RoCE 192.168.100.11:28080, never Tailscale. "
        "Management: Tailscale 100.105.214.61 (gx10-01) and 100.73.238.4 (gx10-02).",
        styles["Body"],
    ))
    story.append(p("Common failures", styles["H"]))
    story.append(p(
        "gx-mini exit 137: raise cgroup memory (24g) and keep ctx at 65536. "
        "Empty restic password: export RESTIC_PASSWORD_FILE. "
        "LiteLLM 401: missing gateway key. "
        "Orchestrator only on loopback: restart after docker0 has an address.",
        styles["Body"],
    ))
    story.append(p("Updating this recipe", styles["H"]))
    story.append(p(
        "Edit files in gx-backup, run scripts/secret-scan.py, commit, push the private repo. "
        "Never commit .env, restic-password, age.key, or SSH private keys.",
        styles["Body"],
    ))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(OUT),
        pagesize=letter,
        title="GX Backup Restore Guide",
        author="GX Backup",
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
    ).build(story)
    print("wrote", OUT, "bytes", OUT.stat().st_size)


if __name__ == "__main__":
    main()
