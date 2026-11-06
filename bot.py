import discord
from discord.ext import commands, tasks
import json
import os
import asyncio
import logging
import imageio_ffmpeg
import wave
import sqlite3
from datetime import datetime

# Путь к файлу тишины (создастся автоматически)
SILENT_WAV_PATH = "silent.wav"
DB_PATH = "bot_database.db"

# --- Инициализация Базы Данных ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # 1. Таблица для записи времени каждые 3 секунды
    c.execute('''CREATE TABLE IF NOT EXISTS heartbeat_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    # 2. Таблица для последних 10 использований
    c.execute('''CREATE TABLE IF NOT EXISTS usage_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_name TEXT,
                    command_name TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    # 3. Таблица для логов войса и киков
    c.execute('''CREATE TABLE IF NOT EXISTS voice_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT,
                    details TEXT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )''')
    conn.commit()
    conn.close()

def ensure_silent_wav(path: str = SILENT_WAV_PATH, duration: float = 5.0, sample_rate: int = 48000):
    if os.path.exists(path):
        return
    n_channels = 2
    sampwidth = 2
    n_frames = int(sample_rate * duration)
    with wave.open(path, 'wb') as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        wf.writeframes(b'\x00' * n_frames * n_channels * sampwidth)
    logging.info(f"✅ Создан {path} ({duration}с тишины, {n_frames} сэмплов)")

# Получаем путь к FFmpeg
FFMPEG_EXECUTABLE = imageio_ffmpeg.get_ffmpeg_exe()
logging.info(f"Используется FFmpeg: {FFMPEG_EXECUTABLE}")

# --- Настройки ---
DATA_FILE = "bot_data.json"
VERSION = "2.5.0"
LAST_UPDATE = "06.11.2026"
IMAGE_PATH = "bot_image.png"
PLACEHOLDER_PATH = "placeholder.png"
AUTO_DELETE_DELAY = 15

logging.basicConfig(level=logging.INFO)

# --- Загрузка/сохранение данных JSON ---
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    else:
        default_data = {
            "guild_id": 1534957471712411789,
            "owner": "plel572",
            "tracked_channels": {},
            "exceptions": {"users": [], "roles": []},
            "moderators": {"users": [], "roles": [], "usernames": []}
        }
        save_data(default_data)
        return default_data

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

data = load_data()
TOKEN = os.getenv("DISCORD_TOKEN")
YOUR_GUILD_ID = data["guild_id"]
OWNER_NAME = data["owner"]

tracked_channels = data["tracked_channels"]
exceptions_users = set(data["exceptions"]["users"])
exceptions_roles = set(data["exceptions"]["roles"])
moderators_users = set(data["moderators"]["users"])
moderators_roles = set(data["moderators"]["roles"])
moderators_usernames = set(data["moderators"]["usernames"])

def save_all():
    data["tracked_channels"] = tracked_channels
    data["exceptions"]["users"] = list(exceptions_users)
    data["exceptions"]["roles"] = list(exceptions_roles)
    data["moderators"]["users"] = list(moderators_users)
    data["moderators"]["roles"] = list(moderators_roles)
    data["moderators"]["usernames"] = list(moderators_usernames)
    save_data(data)

def is_moderator_or_owner(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if user.name == OWNER_NAME: return True
    if user.id in moderators_users: return True
    if user.name in moderators_usernames: return True
    for role in user.roles:
        if role.id in moderators_roles: return True
    return False

# --- Автоудаление ---
async def delete_after_delay(msg, delay):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

async def send_response(interaction, content=None, embed=None, file=None, ephemeral=False, **kwargs):
    try:
        send_kwargs = {"ephemeral": ephemeral}
        if content is not None: send_kwargs["content"] = content
        if embed is not None: send_kwargs["embed"] = embed
        if file is not None: send_kwargs["file"] = file
        send_kwargs.update(kwargs)

        if not interaction.response.is_done():
            await interaction.response.send_message(**send_kwargs)
            msg = await interaction.original_response()
        else:
            send_kwargs.pop("ephemeral", None)
            msg = await interaction.followup.send(**send_kwargs)

        asyncio.create_task(delete_after_delay(msg, AUTO_DELETE_DELAY))
        return msg
    except Exception as e:
        logging.error(f"Ошибка при отправке сообщения: {type(e).__name__}: {e}")
        return None

# --- Бот ---
intents = discord.Intents.default()
intents.message_content = True
intents.messages = True
intents.guilds = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)
bot.remove_command('help')

# --- Фоновая задача для записи в БД каждые 3 секунды ---
@tasks.loop(seconds=3)
async def heartbeat_db_logger():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO heartbeat_log DEFAULT VALUES")
        # Ограничиваем размер базы, храним последние 5000 записей
        c.execute("DELETE FROM heartbeat_log WHERE id NOT IN (SELECT id FROM heartbeat_log ORDER BY id DESC LIMIT 5000)")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка записи в БД (heartbeat): {e}")

# --- Сохранение последних 10 использований ---
@bot.event
async def on_app_command_completion(interaction: discord.Interaction, command: discord.app_commands.Command):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO usage_history (user_name, command_name) VALUES (?, ?)", (str(interaction.user), command.name))
        # Храним только последние 10 записей
        c.execute("DELETE FROM usage_history WHERE id NOT IN (SELECT id FROM usage_history ORDER BY id DESC LIMIT 10)")
        conn.commit()
        conn.close()
    except Exception as e:
        logging.error(f"Ошибка записи в БД (usage): {e}")

# --- Защита от кика ---
@bot.event
async def on_member_remove(member):
    if member.id == bot.user.id:
        guild = member.guild
        await asyncio.sleep(2)
        try:
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target.id == bot.user.id:
                    kicker = entry.user
                    is_exception = kicker.id in exceptions_users or any(r.id in exceptions_roles for r in kicker.roles)
                    is_bot_owner = kicker.name == OWNER_NAME or kicker.name in moderators_usernames
                    is_server_owner = kicker.id == guild.owner_id
                    
                    conn = sqlite3.connect(DB_PATH)
                    c = conn.cursor()
                    
                    if is_exception:
                        logging.info(f"Меня выгнал {kicker.name}. НО он находится в списке исключений бота. Не выгоняю. Записываю данные в свою базу...")
                        c.execute("INSERT INTO voice_logs (event, details) VALUES (?, ?)", ("Kick_Protected_Exception", kicker.name))
                    elif is_bot_owner:
                        logging.info(f"Меня выгнал {kicker.name}. НО он Создатель бота. Не выгоняю. Записываю данные в свою базу...")
                        c.execute("INSERT INTO voice_logs (event, details) VALUES (?, ?)", ("Kick_Protected_Owner", kicker.name))
                    elif is_server_owner:
                        logging.info(f"Меня выгнал {kicker.name}. НО он Владелец этого Discord сервера. Не могу выгнать. Записываю данные в свою базу...")
                        c.execute("INSERT INTO voice_logs (event, details) VALUES (?, ?)", ("Kick_Protected_ServerOwner", kicker.name))
                    else:
                        logging.info(f"Меня выгнал {kicker.name}. Сейчас я его выгоню из сервера через несколько секунд...")
                        c.execute("INSERT INTO voice_logs (event, details) VALUES (?, ?)", ("Kick_Retaliation", kicker.name))
                        conn.commit()
                        conn.close()
                        
                        await asyncio.sleep(3)
                        try:
                            await kicker.kick(reason="Ответная месть за кик бота.")
                        except discord.Forbidden:
                            logging.warning(f"Не удалось выгнать {kicker.name}: нет прав.")
                        except Exception as e:
                            logging.error(f"Ошибка при попытке кика {kicker.name}: {e}")
                        return
                    
                    conn.commit()
                    conn.close()
                    break
        except Exception as e:
            logging.error(f"Ошибка чтения аудит-лога: {e}")

# --- Умная логика нахождения в войсе ---
waiting_voice_channels = {}  # {guild_id: channel_id}

@bot.event
async def on_voice_state_update(member, before, after):
    # 1. Отключение самого бота
    if member.id == bot.user.id:
        if before.channel and not after.channel:
            channel = before.channel
            if waiting_voice_channels.get(channel.guild.id) == channel.id:
                return
                
            logging.info("Связь разорвана.")
            users_in_channel = [m for m in channel.members if not m.bot]
            
            if len(users_in_channel) >= 1:
                logging.info("Если в войсе есть любой один пользователь, то переподключусь обратно...")
                await asyncio.sleep(2)
                try:
                    await channel.connect(self_deaf=True, self_mute=True)
                    start_keepalive(channel.guild.voice_client, interval=60)
                except Exception as e:
                    logging.error(f"Не удалось переподключиться: {e}")
            else:
                logging.info("Иначе: записываю данные о войсе в базу данных.")
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO voice_logs (event, details) VALUES (?, ?)", ("Voice_Disconnect_Empty", str(channel.id)))
                conn.commit()
                conn.close()
        return

    # 2. Логика для пользователей
    guild = member.guild
    voice_client = guild.voice_client

    if voice_client and voice_client.channel:
        channel = voice_client.channel
        if before.channel == channel or after.channel == channel:
            users = [m for m in channel.members if not m.bot]
            if len(users) >= 2:
                logging.info(f"В войсе {len(users)} пользователя. Бот выходит и ждет, пока останется один.")
                waiting_voice_channels[guild.id] = channel.id
                await voice_client.disconnect(force=True)

    elif guild.id in waiting_voice_channels:
        chan_id = waiting_voice_channels[guild.id]
        target_channel = guild.get_channel(chan_id)
        
        if target_channel:
            users = [m for m in target_channel.members if not m.bot]
            if len(users) == 1:
                logging.info("Остался 1 человек. Бот заходит обратно.")
                del waiting_voice_channels[guild.id]
                try:
                    await target_channel.connect(self_deaf=True, self_mute=True)
                    start_keepalive(guild.voice_client, interval=60)
                except Exception as e:
                    logging.error(f"Не удалось зайти обратно: {e}")
            elif len(users) == 0:
                logging.info("В войсе никого не осталось. Отменяю ожидание.")
                del waiting_voice_channels[guild.id]
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("INSERT INTO voice_logs (event, details) VALUES (?, ?)", ("Voice_Abandoned", str(chan_id)))
                conn.commit()
                conn.close()


# --- Keepalive ---
_keepalive_tasks: dict[int, asyncio.Task] = {}

async def keep_voice_alive(voice_client: discord.VoiceClient, interval: int = 60):
    guild_name = voice_client.guild.name
    logging.info(f"🔇 Keepalive запущен для '{guild_name}' (интервал {interval}с)")
    try:
        while voice_client.is_connected():
            if not voice_client.is_playing():
                try:
                    source = discord.FFmpegPCMAudio(
                        SILENT_WAV_PATH,
                        executable=FFMPEG_EXECUTABLE,
                        options='-loglevel error'
                    )
                    voice_client.play(source)
                    logging.debug(f"🔇 Keepalive: отправлена тишина → '{guild_name}'")
                except Exception as e:
                    logging.error(f"Keepalive: ошибка: {e}")
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        logging.info(f"🔇 Keepalive остановлен для '{guild_name}'")

def start_keepalive(voice_client: discord.VoiceClient, interval: int = 60):
    if voice_client is None or not voice_client.is_connected():
        return
    guild_id = voice_client.guild.id
    old = _keepalive_tasks.get(guild_id)
    if old and not old.done():
        old.cancel()
    _keepalive_tasks[guild_id] = asyncio.create_task(keep_voice_alive(voice_client, interval))

def stop_keepalive(guild_id: int):
    task = _keepalive_tasks.pop(guild_id, None)
    if task and not task.done():
        task.cancel()

# --- MusicPlayer & YTDL ---
import yt_dlp

YTDL_OPTS = {
    'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'noplaylist': False,
    'cookiefile': 'cookies.txt',
    'socket_timeout': 15,      # <-- Таймаут сети (в секундах)
    'retries': 3,              # <-- Количество попыток
    'extractor_args': {
        'youtube': {
            'player_client': ['android', 'web'],
        },
        'youtubepot-bgutilscript': {
            # Укажите ПОЛНЫЙ путь к папке 'server' внутри склонированного репозитория
            'server_home': '/полный/путь/до/bgutil-ytdlp-pot-provider/server' 
        }
    },
}

YTDL_PLAYLIST_OPTS = { **YTDL_OPTS, 'extract_flat': 'in_playlist', 'noplaylist': False }
FFMPEG_OPTS = { 'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5', 'options': '-vn' }

music_players: dict[int, "MusicPlayer"] = {}

class MusicPlayer:
    def __init__(self, guild: discord.Guild, voice_client: discord.VoiceClient):
        self.guild = guild
        self.voice_client = voice_client
        self.queue: list[dict] = []
        self.current: dict | None = None
        self.loop_mode: str = "off"
        self.text_channel: discord.TextChannel | None = None

    async def play_next(self):
        if self.voice_client.is_playing() or self.voice_client.is_paused(): return
        if not self.queue: return

        self.current = self.queue.pop(0)
        try:
            def fetch_stream():
                with yt_dlp.YoutubeDL(YTDL_OPTS) as ydl:
                    return ydl.extract_info(self.current['url'], download=False)
            
            info = await asyncio.to_thread(fetch_stream)
            stream_url = info['url']
        except Exception as e:
            logging.error(f"Ошибка извлечения потока для {self.current.get('title')}: {e}")
            await self.play_next()  # пропускаем битый трек
            return

        source = discord.FFmpegPCMAudio(stream_url, executable=FFMPEG_EXECUTABLE, **FFMPEG_OPTS)
        self.voice_client.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(self._after_play(e), bot.loop))
        logging.info(f"▶️ Воспроизведение: {self.current.get('title', 'Unknown')}")

    async def _after_play(self, error):
        if error: logging.error(f"Ошибка воспроизведения: {error}")
        if self.loop_mode == "one" and self.current: self.queue.insert(0, self.current)
        elif self.loop_mode == "all" and self.current: self.queue.append(self.current)
        await self.play_next()

    def add_track(self, track: dict): self.queue.append(track)
    def add_playlist(self, tracks: list[dict]): self.queue.extend(tracks)

# --- Команды ---
@bot.tree.command(name="info", description="Показать информацию о боте или пользователе")
async def info(interaction: discord.Interaction, user: discord.Member = None):
    if not interaction.guild or interaction.guild.id != YOUR_GUILD_ID:
        await send_response(interaction, "❌ Эта команда работает только на этом сервере.", ephemeral=True)
        return

    if user is None:
        try:
            app_info = await bot.application_info()
            description = app_info.description or "Описание отсутствует"
        except Exception as e:
            description = "Описание недоступно"

        embed = discord.Embed(title="🤖 Информация о боте", color=0x2ECC71)
        embed.add_field(name="Название", value=str(bot.user.display_name), inline=False)
        embed.add_field(name="ID", value=str(bot.user.id), inline=False)
        embed.add_field(name="Описание", value=str(description), inline=False)
        embed.add_field(name="Последнее обновление", value=str(LAST_UPDATE), inline=False)
        embed.add_field(name="Версия", value=str(VERSION), inline=False)
        try:
            avatar_url = bot.user.display_avatar.url if bot.user.display_avatar else None
            if avatar_url: embed.set_footer(text="Yellow Bot", icon_url=str(avatar_url))
            else: embed.set_footer(text="Yellow Bot")
        except: embed.set_footer(text="Yellow Bot")

        if IMAGE_PATH and os.path.exists(IMAGE_PATH):
            try:
                file = discord.File(IMAGE_PATH, filename="bot_image.png")
                embed.set_image(url="attachment://bot_image.png")
                await send_response(interaction, embed=embed, file=file)
            except: await send_response(interaction, embed=embed)
        else: await send_response(interaction, embed=embed)
        return

    try: display_name = str(user.display_name)
    except: display_name = "Неизвестно"
    try: username = str(user.name)
    except: username = "Неизвестно"
    try: user_id = str(user.id)
    except: user_id = "?"
    try:
        roles_list = [r.mention for r in user.roles if r.name != "@everyone"]
        roles_str = ", ".join(roles_list) if roles_list else "Нет ролей"
    except: roles_str = "Не удалось получить"
    try:
        mute_role_id = 1535856939760746526
        mute = any((r.id == mute_role_id or r.name == "@замутчены") for r in user.roles)
    except: mute = False

    embed = discord.Embed(title="👤 Информация о пользователе", color=0x2ECC71)
    embed.add_field(name="Никнейм", value=display_name, inline=False)
    embed.add_field(name="Username", value=f"@{username}", inline=False)
    embed.add_field(name="ID", value=user_id, inline=False)
    embed.add_field(name="Роли", value=roles_str, inline=False)
    embed.add_field(name="Mute", value="✅ Да" if mute else "❌ Нет", inline=False)

    attached_file = None
    avatar_set = False
    try:
        if user.display_avatar:
            url = user.display_avatar.url
            if url: embed.set_thumbnail(url=str(url)); avatar_set = True
    except: pass

    if not avatar_set:
        if PLACEHOLDER_PATH and os.path.exists(PLACEHOLDER_PATH):
            try:
                attached_file = discord.File(PLACEHOLDER_PATH, filename="placeholder.png")
                embed.set_thumbnail(url="attachment://placeholder.png")
            except: pass

    try:
        req = interaction.user
        req_name = getattr(req, "display_name", None) or "пользователем"
        req_url = req.display_avatar.url if req and getattr(req, "display_avatar", None) else None
        if req_url: embed.set_footer(text=f"Запросил {req_name}", icon_url=str(req_url))
        else: embed.set_footer(text=f"Запросил {req_name}")
    except: embed.set_footer(text="Запрошено пользователем")

    if attached_file: result = await send_response(interaction, embed=embed, file=attached_file)
    else: result = await send_response(interaction, embed=embed)

    if result is None:
        text = f"**Информация о пользователе**\nНикнейм: {display_name}\nUsername: @{username}\nID: {user_id}\nРоли: {roles_str}\nMute: {'Да' if mute else 'Нет'}"
        await send_response(interaction, text, ephemeral=True)


@bot.tree.command(name="addyellowbot", description="Добавить канал для отслеживания слова/фразы")
async def add(interaction: discord.Interaction, channel: discord.TextChannel, phrase: str):
    if interaction.guild.id != YOUR_GUILD_ID: await send_response(interaction, "❌ Не тот сервер.", ephemeral=True); return
    if not is_moderator_or_owner(interaction): await send_response(interaction, "❌ Нет прав.", ephemeral=True); return
    permissions = channel.permissions_for(interaction.guild.me)
    if not permissions.read_messages or not permissions.read_message_history: await send_response(interaction, "❌ Нет прав бота.", ephemeral=True); return
    tracked_channels[str(channel.id)] = phrase
    save_all()
    await send_response(interaction, f"✅ Бот отслеживает канал {channel.mention} на предмет: `{phrase}`", ephemeral=True)

@bot.tree.command(name="help", description="Показать инструкцию по использованию бота")
async def help_slash(interaction: discord.Interaction):
    if interaction.guild.id != YOUR_GUILD_ID: await send_response(interaction, f"❌ Не тот сервер.", ephemeral=True); return
    embed = discord.Embed(title="🤖 Yellow Bot — инструкция", description="Полный список команд.", color=0x2ECC71)
    embed.add_field(name="📌 Общие команды", value="• `/info [пользователь]` — инфо о боте/юзере.\n• `/help` — эта справка.", inline=False)
    embed.add_field(name="🛠 Модераторы", value="• `/addyellowbot` — добавить канал.\n• `/addexceptions` — исключения.\n• `/addmoders` — модераторы.\n• `/voice <ID>` — зайти в войс.\n• `/playmus <URL>` — музыка.", inline=False)
    embed.set_footer(text="Yellow Bot • /help")
    await send_response(interaction, embed=embed)

@bot.tree.command(name="addexceptions", description="Добавить пользователя или роль в исключения")
async def add_exceptions(interaction: discord.Interaction, user: discord.User = None, role: discord.Role = None):
    if interaction.guild.id != YOUR_GUILD_ID: await send_response(interaction, "❌ Не тот сервер.", ephemeral=True); return
    if not is_moderator_or_owner(interaction): await send_response(interaction, "❌ Нет прав.", ephemeral=True); return
    if user is None and role is None: await send_response(interaction, "❌ Укажите пользователя или роль.", ephemeral=True); return
    added = []
    if user: exceptions_users.add(user.id); added.append(f"{user.mention}")
    if role: exceptions_roles.add(role.id); added.append(f"{role.mention}")
    save_all()
    await send_response(interaction, f"✅ Исключения: {', '.join(added)}.", ephemeral=True)

@bot.tree.command(name="addmoders", description="Добавить модератора")
async def add_moderators(interaction: discord.Interaction, user: discord.User = None, role: discord.Role = None, username: str = None):
    if interaction.guild.id != YOUR_GUILD_ID: await send_response(interaction, "❌ Не тот сервер.", ephemeral=True); return
    if not is_moderator_or_owner(interaction): await send_response(interaction, "❌ Нет прав.", ephemeral=True); return
    if user is None and role is None and username is None: await send_response(interaction, "❌ Укажите параметры.", ephemeral=True); return
    added = []
    if user: moderators_users.add(user.id); added.append(f"{user.mention}")
    if role: moderators_roles.add(role.id); added.append(f"{role.mention}")
    if username: moderators_usernames.add(username); added.append(f"'{username}'")
    save_all()
    await send_response(interaction, f"✅ Модераторы: {', '.join(added)}.", ephemeral=True)

@bot.tree.command(name="voice", description="Подключить бота к голосовому каналу")
async def voice_join(interaction: discord.Interaction, channel_id: str):
    if interaction.guild.id != YOUR_GUILD_ID: await send_response(interaction, "❌ Не тот сервер.", ephemeral=True); return
    if not is_moderator_or_owner(interaction): await send_response(interaction, "❌ Нет прав.", ephemeral=True); return
    try: channel_id_int = int(channel_id)
    except ValueError: await send_response(interaction, "❌ ID должен быть числом.", ephemeral=True); return

    channel = interaction.guild.get_channel(channel_id_int)
    if channel is None or not isinstance(channel, discord.VoiceChannel): await send_response(interaction, "❌ Канал не найден.", ephemeral=True); return
    perms = channel.permissions_for(interaction.guild.me)
    if not perms.connect: await send_response(interaction, f"❌ Нет прав на подключение.", ephemeral=True); return

    await send_response(interaction, f"Сейчас зайду в войс ({channel.name}).")
    
    target_region = "rotterdam"
    if perms.manage_channels:
        try:
            await channel.edit(rtc_region=target_region)
            await asyncio.sleep(1.5)
        except: pass

    try:
        if interaction.guild.voice_client: await interaction.guild.voice_client.disconnect(force=True); await asyncio.sleep(0.5)
        await channel.connect(self_deaf=True, self_mute=True, timeout=60.0, reconnect=True)
        start_keepalive(interaction.guild.voice_client, interval=60)
    except Exception as e:
        logging.error(f"❌ Не удалось подключиться: {e}")

@bot.tree.command(name="playmus", description="Воспроизвести видео или плейлист YouTube")
async def playmus(interaction: discord.Interaction, url: str, loop: str = None):
    if interaction.guild.id != YOUR_GUILD_ID: await send_response(interaction, "❌ Не тот сервер.", ephemeral=True); return
    if not interaction.user.voice or not interaction.user.voice.channel: await send_response(interaction, "❌ Вы должны быть в войсе.", ephemeral=True); return

    target_channel = interaction.user.voice.channel
    guild = interaction.guild
    voice_client = guild.voice_client

    if voice_client is None or not voice_client.is_connected():
        try:
            voice_client = await target_channel.connect(self_deaf=False, self_mute=False)
            await asyncio.sleep(1)
        except Exception as e:
            await send_response(interaction, f"❌ Ошибка подключения.", ephemeral=True); return
    elif voice_client.channel != target_channel:
        try: await voice_client.move_to(target_channel); await asyncio.sleep(1)
        except: pass

    player = music_players.get(guild.id)
    if player is None or player.voice_client != voice_client:
        player = MusicPlayer(guild, voice_client)
        music_players[guild.id] = player
    player.text_channel = interaction.channel

    if loop and loop.lower() in ("one", "all"): player.loop_mode = loop.lower()
    elif loop: await send_response(interaction, "⚠️ Режим: `one` или `all`.", ephemeral=True); return

    await send_response(interaction, "🔎 Проверяю ссылку...")
    try:
        # Оборачиваем синхронный вызов в отдельный поток, чтобы не вешать бота!
        def fetch_info():
            with yt_dlp.YoutubeDL(YTDL_PLAYLIST_OPTS) as ydl:
                return ydl.extract_info(url, download=False)
                
        # Ждём результат, но не блокируем event loop
        info = await asyncio.to_thread(fetch_info)
    except Exception as e:
        await send_response(interaction, f"❌ Ошибка yt-dlp. ({e})", ephemeral=True); return

    if 'entries' in info:
        tracks = [{'url': f"https://www.youtube.com/watch?v={e['id']}", 'title': e.get('title', 'Unknown')} for e in info['entries'] if e]
        if not tracks: await send_response(interaction, "❌ Плейлист пуст.", ephemeral=True); return
        player.add_playlist(tracks)
        await send_response(interaction, f"📀 Добавлен плейлист ({len(tracks)} треков).")
    else:
        player.add_track({'url': url, 'title': info.get('title', 'Unknown')})
        await send_response(interaction, f"🎵 Добавлен трек **{info.get('title', 'Unknown')}**.")

    if not voice_client.is_playing() and not voice_client.is_paused(): await player.play_next()

@bot.event
async def on_message(message):
    await bot.process_commands(message)
    if message.guild is None or message.guild.id != YOUR_GUILD_ID: return
    channel_id = str(message.channel.id)
    if channel_id not in tracked_channels: return
    if message.author.id in exceptions_users: return
    for role in message.author.roles:
        if role.id in exceptions_roles: return
    if message.content.strip() != tracked_channels[channel_id]:
        try: await message.delete()
        except: pass

@bot.event
async def on_ready():
    print(f'Бот {bot.user} запущен!')
    if not heartbeat_db_logger.is_running(): heartbeat_db_logger.start()
    print(f"Глобальные команды: {[cmd.name for cmd in bot.tree.get_commands()]}")

@bot.event
async def setup_hook():
    init_db()
    ensure_silent_wav()
    guild = discord.Object(id=YOUR_GUILD_ID)
    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)
    await bot.tree.sync()
    print("✅ Глобальные команды синхронизированы.")

if __name__ == "__main__":
    bot.run(TOKEN)