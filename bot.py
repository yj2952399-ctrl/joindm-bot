"""
新規参加者へDMを送るDiscordボット
設定はすべてスラッシュコマンドから行う（サーバーごとに保存）
コマンドは「サーバー管理」権限または許可ロールのみ実行可能

必須: Developer Portal の Bot タブで SERVER MEMBERS INTENT を ON にすること
"""
import os
import json
import asyncio
import logging
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")  # 任意: 入れるとコマンド反映が即時になる

_BASE_DIR = Path(__file__).resolve().parent
_data_env = os.getenv("DATA_FILE")
DATA_FILE = Path(_data_env).expanduser().resolve() if _data_env else _BASE_DIR / "data" / "guilds.json"

DEFAULT_MESSAGE = "**{server}** に参加してくれてありがとう、{user}！\nまずは下のリンクを読んでね。"
DEFAULT_LINK_LABEL = "リンクを開く"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("joindm")

intents = discord.Intents.default()
intents.members = True   # ← これが無いと on_member_join が動かない（Portal側のONも必須）
intents.guilds = True

DM_SEND_INTERVAL = 1.0    # レート制限対策の送信間隔
QUEUE_WAIT_TIMEOUT = 60.0 # アイドル時にループを回して生存確認


def default_cfg() -> dict:
    return {
        "enabled": True,
        "message": DEFAULT_MESSAGE,
        "link": "",
        "link_label": DEFAULT_LINK_LABEL,
        "log_channel": None,
        "admin_roles": [],
    }


# ---------- ユーティリティ ----------
def render(template: str, member: discord.Member) -> str:
    """プレースホルダを実値に置換"""
    repl = {
        "{user}": member.mention,
        "{username}": member.name,
        "{server}": member.guild.name,
        "{member_count}": f"{member.guild.member_count:,}",
    }
    out = template
    for k, v in repl.items():
        out = out.replace(k, v)
    return out


def build_view(url: str, label: str) -> discord.ui.View | None:
    if not url or not url.startswith(("http://", "https://")):
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=(label or DEFAULT_LINK_LABEL)[:80],
                                    url=url, style=discord.ButtonStyle.link))
    return view


# ---------- Bot ----------
class JoinDM(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.data: dict = {}
        self._save_lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    # --- 永続化 ---
    def load_data(self):
        if DATA_FILE.exists():
            try:
                self.data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                log.info("設定を読み込みました: %s (%d件)", DATA_FILE, len(self.data))
            except (json.JSONDecodeError, OSError):
                log.warning("データファイル破損。空で開始します")
                self.data = {}
        else:
            log.info("データファイル未作成: %s（新規作成します）", DATA_FILE)

    def cfg(self, guild_id: int) -> dict:
        """書き込み用。辞書が無ければ既定値で作る"""
        c = self.data.setdefault(str(guild_id), {})
        for k, v in default_cfg().items():
            c.setdefault(k, v)
        return c

    def get_cfg(self, guild_id: int) -> dict:
        """読み取り専用。データを汚さない"""
        merged = default_cfg()
        merged.update(self.data.get(str(guild_id)) or {})
        if not isinstance(merged.get("admin_roles"), list):
            merged["admin_roles"] = []
        return merged

    async def save(self):
        async with self._save_lock:
            DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = DATA_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            tmp.replace(DATA_FILE)

    # --- 起動 ---
    async def setup_hook(self):
        self.load_data()
        if not self.intents.members:
            raise SystemExit("intents.members が False です。SERVER MEMBERS INTENT を ON にしてください")
        log.info("SERVER MEMBERS INTENT: %s", "ON" if self.intents.members else "OFF")
        log.info("データ保存先: %s", DATA_FILE)

        # 参照を保持しないとGCでタスクが消える
        self._worker_task = asyncio.create_task(self._dm_worker())

        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
            log.info("ギルド %s にコマンドを同期しました", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("グローバルコマンドを同期しました（反映に最大1時間）")

    async def close(self):
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
        await super().close()

    # --- DM送信キュー ---
    async def _dm_worker(self):
        while True:
            try:
                member, cfg = await asyncio.wait_for(self._queue.get(),
                                                     timeout=QUEUE_WAIT_TIMEOUT)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self._send_dm(member, cfg)
            except Exception:
                log.exception("DM処理中に例外")
            finally:
                self._queue.task_done()
            await asyncio.sleep(DM_SEND_INTERVAL)

    async def _send_dm(self, member: discord.Member, cfg: dict):
        if member.guild.get_member(member.id) is None:
            log.warning("スキップ: %s がキャッシュに無い（Members Intent / キャッシュの問題）", member.id)
            return

        text = render(cfg["message"], member)[:2000]
        embed = discord.Embed(description=text, color=0x5865F2)
        if member.guild.icon:
            embed.set_author(name=member.guild.name, icon_url=member.guild.icon.url)
            embed.set_thumbnail(url=member.guild.icon.url)
        else:
            embed.set_author(name=member.guild.name)

        view = build_view(cfg.get("link", ""), cfg.get("link_label", DEFAULT_LINK_LABEL))
        if cfg.get("link") and view is None:
            log.warning("リンクURLが不正なのでボタン無しで送信: %r", cfg.get("link"))

        last_err: Exception | None = None
        for attempt in (1, 2):
            try:
                await member.send(embed=embed, view=view)
            except discord.Forbidden:
                log.warning("DM拒否: %s (%s) — 相手のDM許可設定を確認", member, member.id)
                await self._log_result(member, "DMが拒否されました（DM許可設定の可能性）")
                return
            except discord.HTTPException as e:
                last_err = e
                log.warning("DM送信失敗(%d回目): %s -> %s", attempt, member, e)
                if attempt == 1 and (e.status == 429 or (e.status and e.status >= 500)):
                    await asyncio.sleep(2)
                    continue
                await self._log_result(member, f"送信失敗: {e}")
                return
            else:
                log.info("DM送信OK: %s (%s)", member, member.id)
                await self._log_result(member, None)
                return
        if last_err:
            await self._log_result(member, f"送信失敗: {last_err}")

    async def _log_result(self, member: discord.Member, error: str | None):
        ch_id = self.get_cfg(member.guild.id).get("log_channel")
        if not ch_id:
            return
        ch = member.guild.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            log.warning("ログ先チャンネルが見つかりません: %s", ch_id)
            return
        try:
            if error:
                await ch.send(f"⚠️ {member.mention} へのDM失敗: {error}")
            else:
                await ch.send(f"✅ {member.mention} へDMを送信しました")
        except discord.HTTPException as e:
            log.warning("ログ送信失敗: %s", e)


bot = JoinDM()


@bot.event
async def on_ready():
    log.info("ログイン: %s (id=%s)", bot.user, bot.user.id)
    log.info("参加サーバー数: %d", len(bot.guilds))


@bot.event
async def on_member_join(member: discord.Member):
    log.info("join event: %s (id=%s) guild=%s", member, member.id, member.guild.id)
    if member.bot:
        return
    cfg = bot.get_cfg(member.guild.id)
    if not cfg["enabled"]:
        log.info("DM無効のためスキップ: guild=%s", member.guild.id)
        return
    if not cfg["message"].strip():
        log.info("本文が空のためスキップ: guild=%s", member.guild.id)
        return
    await bot._queue.put((member, cfg))


# ---------- 権限チェック ----------
def is_operator(itx: discord.Interaction) -> bool:
    """管理者 or 許可ロールなら True"""
    perms = itx.user.guild_permissions
    if perms.administrator or perms.manage_guild:
        return True
    allowed = set(bot.get_cfg(itx.guild_id).get("admin_roles") or [])
    if allowed and any(r.id in allowed for r in getattr(itx.user, "roles", [])):
        return True
    return False


# ---------- コマンド ----------
@app_commands.default_permissions(manage_guild=True)
class Config(commands.GroupCog, name="config", description="DM設定（管理者専用）"):
    def __init__(self):
        super().__init__()

    async def __cog_app_commands_check__(self, itx: discord.Interaction) -> bool:
        if itx.guild is None:
            raise app_commands.NoPrivateMessage()
        if is_operator(itx):
            return True
        raise app_commands.MissingPermissions(["manage_guild"])

    @app_commands.command(name="set_message", description="送信するメッセージを設定")
    @app_commands.describe(text="本文。{user} {username} {server} {member_count} が使える")
    @app_commands.guild_only()
    async def set_message(self, itx: discord.Interaction, text: str):
        if len(text) > 2000:
            return await itx.response.send_message("2000文字以内にしてね", ephemeral=True)
        cfg = bot.cfg(itx.guild_id)
        cfg["message"] = text
        await bot.save()
        await itx.response.send_message(
            f"✅ メッセージを更新しました\n```\n{text}\n```", ephemeral=True)

    @app_commands.command(name="set_link", description="DMに載せるリンクを設定")
    @app_commands.describe(url="http(s)から始まるURL", label="ボタンの表示名（任意）")
    @app_commands.guild_only()
    async def set_link(self, itx: discord.Interaction, url: str, label: str | None = None):
        if not url.startswith(("http://", "https://")):
            return await itx.response.send_message("http(s):// から始まるURLを指定してね", ephemeral=True)
        cfg = bot.cfg(itx.guild_id)
        cfg["link"] = url
        if label:
            cfg["link_label"] = label
        await bot.save()
        await itx.response.send_message(f"✅ リンクを設定: {url}", ephemeral=True)

    @app_commands.command(name="clear_link", description="リンクを削除")
    @app_commands.guild_only()
    async def clear_link(self, itx: discord.Interaction):
        cfg = bot.cfg(itx.guild_id)
        cfg["link"] = ""
        await bot.save()
        await itx.response.send_message("🗑 リンクを削除しました", ephemeral=True)

    @app_commands.command(name="toggle", description="DM送信のON/OFF")
    @app_commands.guild_only()
    async def toggle(self, itx: discord.Interaction, enabled: bool):
        cfg = bot.cfg(itx.guild_id)
        cfg["enabled"] = enabled
        await bot.save()
        await itx.response.send_message(
            f"{'🟢 有効' if enabled else '🔴 無効'} にしました", ephemeral=True)

    @app_commands.command(name="log_channel", description="送信結果のログ先チャンネル")
    @app_commands.guild_only()
    async def log_channel(self, itx: discord.Interaction, channel: discord.TextChannel | None = None):
        cfg = bot.cfg(itx.guild_id)
        cfg["log_channel"] = channel.id if channel else None
        await bot.save()
        await itx.response.send_message(
            f"ログ先: {channel.mention if channel else 'なし'}", ephemeral=True)

    @app_commands.command(name="allow_role", description="設定を触れるロールを追加")
    @app_commands.describe(role="許可するロール")
    @app_commands.guild_only()
    async def allow_role(self, itx: discord.Interaction, role: discord.Role):
        cfg = bot.cfg(itx.guild_id)
        roles = set(cfg.get("admin_roles") or [])
        roles.add(role.id)
        cfg["admin_roles"] = list(roles)
        await bot.save()
        await itx.response.send_message(f"✅ {role.mention} を許可しました", ephemeral=True)

    @app_commands.command(name="disallow_role", description="許可ロールを解除")
    @app_commands.describe(role="解除するロール")
    @app_commands.guild_only()
    async def disallow_role(self, itx: discord.Interaction, role: discord.Role):
        cfg = bot.cfg(itx.guild_id)
        roles = set(cfg.get("admin_roles") or [])
        roles.discard(role.id)
        cfg["admin_roles"] = list(roles)
        await bot.save()
        await itx.response.send_message(f"🗑 {role.mention} を解除しました", ephemeral=True)

    @app_commands.command(name="show", description="現在の設定を表示")
    @app_commands.guild_only()
    async def show(self, itx: discord.Interaction):
        cfg = bot.get_cfg(itx.guild_id)
        e = discord.Embed(title="現在の設定", color=0x5865F2)
        e.add_field(name="状態", value="🟢 有効" if cfg["enabled"] else "🔴 無効", inline=True)
        e.add_field(name="リンク", value=cfg["link"] or "未設定", inline=True)
        e.add_field(name="ログ", value=f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "なし", inline=True)
        roles = cfg.get("admin_roles") or []
        e.add_field(name="許可ロール",
                    value=" ".join(f"<@&{r}>" for r in roles) or "なし", inline=False)
        e.add_field(name="本文", value=f"```\n{cfg['message']}\n```", inline=False)
        await itx.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="test", description="自分宛にテスト送信")
    @app_commands.guild_only()
    async def test(self, itx: discord.Interaction):
        await itx.response.defer(ephemeral=True)
        try:
            await bot._send_dm(itx.user, bot.get_cfg(itx.guild_id))
        except Exception as e:
            log.exception("テスト送信で例外")
            return await itx.followup.send(f"テスト送信で例外: {e}", ephemeral=True)
        await itx.followup.send("テストDMを送りました（届かない場合はDM設定を確認）", ephemeral=True)

    @app_commands.command(name="diagnose", description="DMが飛ばない原因を切り分け")
    @app_commands.guild_only()
    async def diagnose(self, itx: discord.Interaction):
        await itx.response.defer(ephemeral=True)
        me = itx.guild.me
        cfg = bot.get_cfg(itx.guild_id)
        lines = [
            f"members intent: {'ON' if bot.intents.members else 'OFF'}",
            f"gateway 接続: {'OK' if bot.is_ws_ratelimited() is False else '要確認'} / latency={bot.latency:.3f}s",
            f"bot ロール位置: {me.top_role.position}",
            f"自分のキャッシュ: {'あり' if itx.guild.get_member(itx.user.id) else 'なし'}",
            f"サーバーのキャッシュ人数: {itx.guild.member_count}",
            f"設定 enabled: {cfg['enabled']} / 本文長: {len(cfg['message'])} / リンク: {cfg['link'] or 'なし'}",
            f"キュー残り: {bot._queue.qsize()}",
            f"ワーカー稼働: {'はい' if bot._worker_task and not bot._worker_task.done() else 'いいえ'}",
        ]
        await itx.followup.send("```\n" + "\n".join(lines) + "\n```", ephemeral=True)

    @app_commands.command(name="reset", description="設定を初期値に戻す")
    @app_commands.guild_only()
    async def reset(self, itx: discord.Interaction):
        bot.data.pop(str(itx.guild_id), None)
        await bot.save()
        await itx.response.send_message("♻️ 設定をリセットしました", ephemeral=True)


# ---------- エラーハンドラ ----------
@bot.tree.error
async def on_app_command_error(itx: discord.Interaction, error: app_commands.AppCommandError):
    err = error
    if isinstance(err, app_commands.CommandInvokeError) and err.original:
        err = err.original
    if isinstance(err, app_commands.CheckAnyFailure) and err.errors:
        err = next((e for e in err.errors if isinstance(e, app_commands.AppCommandError)), err)

    if isinstance(err, app_commands.MissingPermissions):
        msg = "このコマンドは「サーバー管理」権限か、許可されたロールが必要です"
    elif isinstance(err, app_commands.NoPrivateMessage):
        msg = "サーバー内で実行してください"
    elif isinstance(err, app_commands.CommandOnCooldown):
        msg = f"クールダウン中です（{err.retry_after:.1f}秒）"
    else:
        log.error("コマンドエラー: %s", err, exc_info=err)
        msg = "エラーが発生しました"

    try:
        if itx.response.is_done():
            await itx.followup.send(msg, ephemeral=True)
        else:
            await itx.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN が未設定です")

    async with bot:
        await bot.add_cog(Config())
        await bot.login(TOKEN)  # ログインは1回だけ
        delay = 5
        while True:
            try:
                await bot.connect()  # 初回ハンドシェイク失敗(503等)をここで受ける
                break
            except (aiohttp.ClientError, asyncio.TimeoutError,
                    discord.GatewayNotFound, OSError) as e:
                log.warning("Gateway接続失敗: %s → %d秒後に再試行", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())