"""
新規参加者へDMを送るDiscordボット
設定はすべてスラッシュコマンドから行う（サーバーごとに保存）
"""
import os
import json
import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")          # 入れるとコマンド反映が即時になる（任意）
DATA_FILE = Path("data/guilds.json")

DEFAULT_MESSAGE = "**{server}** に参加してくれてありがとう、{user}！\nまずは下のリンクを読んでね。"
DEFAULT_LINK_LABEL = "リンクを開く"

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("joindm")

intents = discord.Intents.default()
intents.members = True          # ← これが無いと on_member_join が動かない
intents.guilds = True


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


class LinkView(discord.ui.View):
    def __init__(self, url: str, label: str):
        super().__init__(timeout=None)
        self.add_item(discord.ui.Button(label=label[:80], url=url,
                                        style=discord.ButtonStyle.link))


# ---------- Bot ----------
class JoinDM(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.data: dict = {}
        self._save_lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()

    # --- 永続化 ---
    def load_data(self):
        if DATA_FILE.exists():
            try:
                self.data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                log.warning("データファイル破損。空で開始します")
                self.data = {}

    def cfg(self, guild_id: int) -> dict:
        c = self.data.setdefault(str(guild_id), {})
        c.setdefault("enabled", True)
        c.setdefault("message", DEFAULT_MESSAGE)
        c.setdefault("link", "")
        c.setdefault("link_label", DEFAULT_LINK_LABEL)
        c.setdefault("log_channel", None)
        return c

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
        asyncio.create_task(self._dm_worker())
        if GUILD_ID:
            g = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    # --- DM送信キュー（レート制限対策で1秒間隔） ---
    async def _dm_worker(self):
        while True:
            member, cfg = await self._queue.get()
            try:
                await self._send_dm(member, cfg)
            except Exception:
                log.exception("DM処理中に例外")
            finally:
                self._queue.task_done()
            await asyncio.sleep(1.0)

    async def _send_dm(self, member: discord.Member, cfg: dict):
        if member.guild.get_member(member.id) is None:
            return  # 送信前に退出した
        text = render(cfg["message"], member)[:2000]
        embed = discord.Embed(description=text, color=0x5865F2)
        if member.guild.icon:
            embed.set_author(name=member.guild.name, icon_url=member.guild.icon.url)
            embed.set_thumbnail(url=member.guild.icon.url)
        else:
            embed.set_author(name=member.guild.name)
        view = LinkView(cfg["link"], cfg["link_label"]) if cfg["link"] else None

        try:
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            await self._log_result(member, "DMが拒否されました（DM許可設定の可能性）")
        except discord.HTTPException as e:
            await self._log_result(member, f"送信失敗: {e}")
        else:
            await self._log_result(member, None)

    async def _log_result(self, member: discord.Member, error: str | None):
        ch_id = self.cfg(member.guild.id).get("log_channel")
        if not ch_id:
            return
        ch = member.guild.get_channel(ch_id)
        if not isinstance(ch, discord.TextChannel):
            return
        try:
            if error:
                await ch.send(f"⚠️ {member.mention} へのDM失敗: {error}")
            else:
                await ch.send(f"✅ {member.mention} へDMを送信しました")
        except discord.HTTPException:
            pass


bot = JoinDM()


@bot.event
async def on_ready():
    log.info("ログイン: %s (id=%s)", bot.user, bot.user.id)


@bot.event
async def on_member_join(member: discord.Member):
    if member.bot:
        return
    cfg = bot.cfg(member.guild.id)
    if not cfg["enabled"] or not cfg["message"].strip():
        return
    await bot._queue.put((member, dict(cfg)))


# ---------- コマンド ----------
class Config(commands.GroupCog, name="config", description="DM設定"):
    def __init__(self):
        super().__init__()

    @app_commands.command(name="set_message", description="送信するメッセージを設定")
    @app_commands.describe(text="本文。{user} {username} {server} {member_count} が使える")
    @app_commands.default_permissions(manage_guild=True)
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
    @app_commands.default_permissions(manage_guild=True)
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
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def clear_link(self, itx: discord.Interaction):
        cfg = bot.cfg(itx.guild_id)
        cfg["link"] = ""
        await bot.save()
        await itx.response.send_message("🗑 リンクを削除しました", ephemeral=True)

    @app_commands.command(name="toggle", description="DM送信のON/OFF")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def toggle(self, itx: discord.Interaction, enabled: bool):
        cfg = bot.cfg(itx.guild_id)
        cfg["enabled"] = enabled
        await bot.save()
        await itx.response.send_message(
            f"{'🟢 有効' if enabled else '🔴 無効'} にしました", ephemeral=True)

    @app_commands.command(name="log_channel", description="送信結果のログ先チャンネル")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def log_channel(self, itx: discord.Interaction, channel: discord.TextChannel | None = None):
        cfg = bot.cfg(itx.guild_id)
        cfg["log_channel"] = channel.id if channel else None
        await bot.save()
        await itx.response.send_message(
            f"ログ先: {channel.mention if channel else 'なし'}", ephemeral=True)

    @app_commands.command(name="show", description="現在の設定を表示")
    @app_commands.guild_only()
    async def show(self, itx: discord.Interaction):
        cfg = bot.cfg(itx.guild_id)
        e = discord.Embed(title="現在の設定", color=0x5865F2)
        e.add_field(name="状態", value="🟢 有効" if cfg["enabled"] else "🔴 無効", inline=True)
        e.add_field(name="リンク", value=cfg["link"] or "未設定", inline=True)
        e.add_field(name="ログ", value=f"<#{cfg['log_channel']}>" if cfg["log_channel"] else "なし", inline=True)
        e.add_field(name="本文", value=f"```\n{cfg['message']}\n```", inline=False)
        await itx.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="test", description="自分宛にテスト送信")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def test(self, itx: discord.Interaction):
        await itx.response.defer(ephemeral=True)
        await bot._send_dm(itx.user, dict(bot.cfg(itx.guild_id)))
        await itx.followup.send("テストDMを送りました（届かない場合はDM設定を確認）", ephemeral=True)

    @app_commands.command(name="reset", description="設定を初期値に戻す")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def reset(self, itx: discord.Interaction):
        bot.data.pop(str(itx.guild_id), None)
        await bot.save()
        await itx.response.send_message("♻️ 設定をリセットしました", ephemeral=True)


async def main():
    async with bot:
        await bot.add_cog(Config())
        await bot.start(TOKEN)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN が未設定です")
    asyncio.run(main())