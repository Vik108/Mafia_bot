import asyncio
import json
import logging
import os
import random
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

ROLES = ["Мафия", "Доктор", "Комиссар", "Мирный"]
MIN_PLAYERS = 4
STATS_FILE = "stats.json"

@dataclass
class Game:
    chat_id: int
    players: Dict[int, str] = field(default_factory=dict)  # user_id -> name
    alive: Set[int] = field(default_factory=set)
    roles: Dict[int, str] = field(default_factory=dict)  # user_id -> role
    phase: str = "registration"  # registration | night | day | ended
    game_number: int = 0
    mafia_target: Optional[int] = None
    doctor_target: Optional[int] = None
    commissar_target: Optional[int] = None
    day_votes: Dict[int, int] = field(default_factory=dict)  # voter_id -> target_id
    started: bool = False
    status_message_id: Optional[int] = None
    status_task: Optional[asyncio.Task] = None
    instruction_message_id: Optional[int] = None
    timer_task: Optional[asyncio.Task] = None
    last_activity: float = 0.0
    status_delay: int = 60
    mafia_kills: Dict[int, int] = field(default_factory=dict)  # user_id -> kills count

    def reset_night_actions(self) -> None:
        self.mafia_target = None
        self.doctor_target = None
        self.commissar_target = None

    def reset_day_votes(self) -> None:
        self.day_votes = {}

class GameManager:
    def __init__(self) -> None:
        self.games: Dict[int, Game] = {}

    def new_game(self, chat_id: int) -> Game:
        game = Game(chat_id=chat_id)
        self.games[chat_id] = game
        return game

    def get(self, chat_id: int) -> Optional[Game]:
        return self.games.get(chat_id)

    def end_game(self, chat_id: int) -> None:
        if chat_id in self.games:
            self.games[chat_id].phase = "ended"
            task = self.games[chat_id].status_task
            if task and not task.done():
                task.cancel()
            self.games[chat_id].status_task = None
            timer = self.games[chat_id].timer_task
            if timer and not timer.done():
                timer.cancel()
            self.games[chat_id].timer_task = None

ROLE_EMOJI = {
    "Мафия": "😈",
    "Доктор": "🩺",
    "Комиссар": "🕵️",
    "Мирный": "🙂",
}
STICKERS = [
    "CAACAgIAAxkBAAEGZtNnG_ygliDUYwQhumM6",  # мафия маска (pack: MafiaDuck)
    "CAACAgIAAxkBAAEGZtdnG_z1MchGpcxMClLZ",  # револьвер
    "CAACAgIAAxkBAAEGZtpnG_0Jg8pN9LeYcL9D",  # сигара
    "CAACAgIAAxkBAAEGZt1nG_0vLrXwdkKDLteY",  # шляпа
    "CAACAgIAAxkBAAEGZuBnG_1PHviPkf2QEUpB",  # ночь/луна
    "CAACAgIAAxkBAAEGZuJnG_1m2VLQwS_WwoMP",  # доктор
    "CAACAgIAAxkBAAEGZuRnG_1vSdaPwgswqY4F",  # комиссар/лупа
    "CAACAgIAAxkBAAEGZuZnG_2Jk-y7RFBZSFHg",  # карта/кровь
    "CAACAgIAAxkBAAEGZuhnG_2cGKnz03-v3KKy",  # нож
    "CAACAgIAAxkBAAEGZupnG_2o9n7GlL-1n1ia",  # суд
    "CAACAgIAAxkBAAEGZu1nG_3FksXlXxQs7auX",  # молчание
    "CAACAgIAAxkBAAEGZu9nG_3YTZYBFvToNiOf",  # ночь-город
    "CAACAgIAAxkBAAEGZvFnG_30aCPGK8HBXHi5",  # факел
    "CAACAgIAAxkBAAEGZvNnG_4KahJg4g_s8FFR",  # карта2
    "CAACAgIAAxkBAAEGZvVnG_4fLM0k90un0nLB",  # ожидание
]
MAFIA_EMOJI = ["😈", "🩺", "🕵️", "🌙", "🌤", "⌛", "👁️", "🔪", "🎭"]

class StatsManager:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data: Dict[str, Dict] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            self.data = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def _save(self) -> None:
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def next_game_number(self, chat_id: int) -> int:
        chat_key = str(chat_id)
        chat_stats = self.data.setdefault(chat_key, {"games": 0, "users": {}})
        chat_stats["games"] += 1
        self._save()
        return chat_stats["games"]

    def record_result(self, chat_id: int, players: Dict[int, str], winners: Set[int], roles: Dict[int, str]) -> None:
        chat_key = str(chat_id)
        chat_stats = self.data.setdefault(chat_key, {"games": 0, "users": {}})
        users = chat_stats.setdefault("users", {})
        for uid, name in players.items():
            user_key = str(uid)
            entry = users.setdefault(user_key, {"name": name, "played": 0, "wins": 0, "roles": {}})
            entry["name"] = name  # обновляем последнее имя
            entry["played"] += 1
            if uid in winners:
                entry["wins"] += 1
            role = roles.get(uid)
            if role:
                role_counts = entry["roles"]
                role_counts[role] = role_counts.get(role, 0) + 1
        self._save()

    def get_stats_text(self, chat_id: int) -> str:
        chat_key = str(chat_id)
        chat_stats = self.data.get(chat_key)
        if not chat_stats:
            return "Статистика пуста."
        lines = [f"Партия №{chat_stats.get('games', 0)}"]
        users = chat_stats.get("users", {})
        if not users:
            lines.append("Нет данных по игрокам.")
        else:
            def best_role(roles: Dict[str, int]) -> str:
                if not roles:
                    return ""
                role = max(roles.items(), key=lambda x: x[1])[0]
                return f"{ROLE_EMOJI.get(role, '')} {role}"

            sorted_users = sorted(users.items(), key=lambda x: x[1].get("wins", 0), reverse=True)
            for _, info in sorted_users[:15]:
                role_mark = best_role(info.get("roles", {}))
                lines.append(
                    f"{info.get('name', 'Игрок')}: {info.get('wins', 0)} побед из {info.get('played', 0)}"
                    + (f" | чаще: {role_mark}" if role_mark else "")
                )
        return "\n".join(lines)

game_manager = GameManager()
stats_manager = StatsManager(STATS_FILE)
bot: Bot
dp = Dispatcher()

def phase_prefix(game: Optional[Game] = None, phase: Optional[str] = None) -> str:
    phase_name = phase or (game.phase if game else None)
    return {
        "registration": "📋",
        "night": "🌙",
        "day": "🌤",
        "ended": "🏁",
    }.get(phase_name, "🎭")

async def send_chat(game: Game, text: str, phase: Optional[str] = None) -> None:
    prefix = phase_prefix(game, phase)
    try:
        await bot.send_message(game.chat_id, f"{prefix} {text}")
    except Exception:
        pass

async def send_random_sticker(game: Game) -> None:
    if not STICKERS:
        if MAFIA_EMOJI:
            await send_chat(game, random.choice(MAFIA_EMOJI))
        return
    try:
        sticker = random.choice(STICKERS)
        await bot.send_sticker(game.chat_id, sticker)
    except Exception:
        if MAFIA_EMOJI:
            await send_chat(game, random.choice(MAFIA_EMOJI))

def is_group(message: Message) -> bool:
    return message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}

def touch_activity(chat_id: int) -> None:
    game = game_manager.get(chat_id)
    if game:
        game.last_activity = asyncio.get_event_loop().time()
        game.status_delay = 60

def build_player_kb(game: Game, prefix: str, include_abstain: bool = False) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for user_id in game.alive:
        name = game.players.get(user_id, "Игрок")
        kb.button(text=name, callback_data=f"{prefix}:{game.chat_id}:{user_id}")
    if include_abstain:
        kb.button(text="🚫 Воздержаться", callback_data=f"{prefix}:{game.chat_id}:-1")
    kb.adjust(2)
    return kb

def check_victory(game: Game) -> Optional[str]:
    alive_roles = [game.roles.get(uid, "") for uid in game.alive]
    mafia_alive = alive_roles.count("Мафия")
    town_alive = len(game.alive) - mafia_alive
    if mafia_alive == 0:
        return "Победа мирных! Мафия ликвидирована."
    if mafia_alive >= town_alive:
        return "Победа мафии! Мирные в меньшинстве."
    return None

def build_status_text(game: Game) -> str:
    phase_emoji = {"registration": "📋", "night": "🌙", "day": "🌤", "ended": "🏁"}.get(game.phase, "ℹ️")
    lines = [f"{phase_emoji} Партия №{game.game_number or '-'} | Фаза: {game.phase}"]
    alive_names = [game.players.get(uid, "Игрок") for uid in game.alive]
    all_names = [game.players.get(uid, "Игрок") for uid in game.players]
    lines.append(f"Игроки: {', '.join(all_names) if all_names else '—'}")
    lines.append(f"Живы: {', '.join(alive_names) if alive_names else '—'}")
    if game.phase == "registration":
        need = max(0, MIN_PLAYERS - len(game.players))
        if need > 0:
            phrases = [
                "Идёт набор в мафию! Осталось {n} смельчаков. Неужели все боятся роли шерифа? 😏 Присоединяйся!",
                "Мафия зовёт! Для старта нужно ещё {n} игроков. Кто будет доктором? Или все уже в тени мафии прячутся? 🕵️",
                "Набор продолжается... Осталось {n} мест. Если не наберём, мафия сама нас всех убьёт от скуки! 😂 Вступай!",
                "Эй, мафиози и честные граждане! Нужно {n} для полной команды. Не томи, жми 'присоединиться' — или ты шпион? 😉",
                "Идёт поиск игроков в мафию. Осталось {n}. Без тебя мирный житель останется без защиты! 🦸‍♀️",
                "Мафия на старте! Ещё {n} нужно. Все сидят в чате и ждут? Давай, не стесняйся — стань звездой ночи! 🌟",
                "Набор в мафию: {n} на подходе. Если никто не придёт, придётся играть с ботами... А они всегда мафия! 🤖",
                "Осталось {n} для запуска мафии. Неужели никто не хочет разоблачить предателей? Твоя роль ждёт героя! 👤",
                "Мафия набирает обороты! Нужно {n}. Соберём банду и устроим интриги — жми кнопку! 🎭",
                "Идёт набор: {n} игроков до финиша. Если опоздаешь, мафия без тебя победит... Шучу, но seriously — присоединяйся! 😜",
            ]
            lines = [random.choice(phrases).format(n=need)]
    elif game.phase == "night":
        pending = []
        mafia_alive = any(role == "Мафия" and uid in game.alive for uid, role in game.roles.items())
        doctor_alive = any(role == "Доктор" and uid in game.alive for uid, role in game.roles.items())
        commissar_alive = any(role == "Комиссар" and uid in game.alive for uid, role in game.roles.items())
        if mafia_alive and game.mafia_target is None:
            pending.append("Мафия")
        if doctor_alive and game.doctor_target is None:
            pending.append("Доктор")
        if commissar_alive and game.commissar_target is None:
            pending.append("Комиссар")
        if pending:
            lines.append("Ждём действия: " + ", ".join(pending))
    elif game.phase == "day":
        waiting_votes = [uid for uid in game.alive if uid not in game.day_votes]
        if waiting_votes:
            names = [game.players.get(uid, "Игрок") for uid in waiting_votes]
            lines.append("Ждём голос: " + ", ".join(names))
    return "\n".join(lines)

async def send_game_report(game: Game, win_text: str):
    role_emoji = ROLE_EMOJI
    all_players = []
    mafia_list = []
    
    # Собираем информацию о всех игроках
    for uid, name in game.players.items():
        role = game.roles.get(uid, 'неизвестно')
        icon = role_emoji.get(role, '')
        status = '🟩' if uid in game.alive else '💀'
        line = f"{icon} {name} — {role} {status}"
        all_players.append(line)
        if role == "Мафия":
            mafia_list.append(uid)
    
    # Определяем героя партии и генерируем шутку
    mvp_text = ""
    if mafia_list and 'мафии' in win_text.lower():
        # Находим мафиози с наибольшим количеством убийств
        best_mafia = None
        max_kills = 0
        for mafia_id in mafia_list:
            kills = game.mafia_kills.get(mafia_id, 0)
            if kills > max_kills:
                max_kills = kills
                best_mafia = mafia_id
        
        if best_mafia and max_kills > 0:
            mafia_phrases = [
                f"Герой партии — {game.players.get(best_mafia)}! Этот мафиози устроил настоящую резню 😈",
                f"Звезда ночи — {game.players.get(best_mafia)}! Убил {max_kills} {'игрока' if max_kills == 1 else 'игроков' if max_kills < 5 else 'игроков'}! 🔪",
                f"Король тьмы — {game.players.get(best_mafia)}! {max_kills} {'душ' if max_kills == 1 else 'душ' if max_kills < 5 else 'душ'} отправлены в небытие 🌙",
                f"Мастер интриг — {game.players.get(best_mafia)}! Холоднокровно устранил {max_kills} {'соперника' if max_kills == 1 else 'соперников' if max_kills < 5 else 'соперников'}! 🎭"
            ]
            mvp_text = random.choice(mafia_phrases)
        else:
            mvp_text = f"Герой партии — {game.players.get(mafia_list[0])}! Этот мафиози был особенно опасен 😈"
    
    elif mafia_list and 'мирных' in win_text.lower():
        good_players = [uid for uid in game.players if game.roles.get(uid) != 'Мафия']
        if good_players:
            mvp = random.choice(good_players)
            role = game.roles.get(mvp, 'Мирный')
            
            if role == "Доктор":
                doctor_phrases = [
                    f"Ангел-хранитель — {game.players.get(mvp)}! Спас столько жизней, что deserves a Nobel prize! 🩺",
                    f"Чудо-доктор — {game.players.get(mvp)}! Даже мафия уважает его навыки! 💉",
                    f"Главный медбрат — {game.players.get(mvp)}! Без него город бы пал! 🏥"
                ]
                mvp_text = random.choice(doctor_phrases)
            elif role == "Комиссар":
                commissar_phrases = [
                    f"Орёл правосудия — {game.players.get(mvp)}! Раскрыл все мафиозные схемы! 🕵️",
                    f"Шериф города — {game.players.get(mvp)}! Его лупа видит всё! 🔍",
                    f"Страж порядка — {game.players.get(mvp)}! Мафия дрожит при одном его имени! ⚖️"
                ]
                mvp_text = random.choice(commissar_phrases)
            else:
                peaceful_phrases = [
                    f"Герой партии — {game.players.get(mvp)}! Настоящий защитник города! 🕊️",
                    f"Светоч правды — {game.players.get(mvp)}! Его голос был решающим! 🌟",
                    f"Душа города — {game.players.get(mvp)}! Мирные победили благодаря ему! 🏆"
                ]
                mvp_text = random.choice(peaceful_phrases)
    
    else:
        mvp = next(iter(game.players), None)
        if mvp:
            neutral_phrases = [
                f"Герой партии — {game.players.get(mvp)}! Может, в следующий раз он сменит роль? 🤔",
                f"Загадочный игрок — {game.players.get(mvp)}! Кто знает, что творилось в его голове? 🎭",
                f"Тёмная лошадка — {game.players.get(mvp)}! Интрига остаётся до конца! 🌫️"
            ]
            mvp_text = random.choice(neutral_phrases)
    
    # Разнообразные завершающие фразы
    phrases = [
        "Реванш? 😉", "Лучшие интриги вечера!", "Достойная партия!",
        "Мафия уходит в подполье... до следующей игры!", "Спасибо всем за эмоции!",
        "Ночь была длинной, а игра — горячей! 🔥", "Кто следующий в списке жертв? 😈",
        "Город спит спокойно... пока не начнётся новая игра! 🌃", "Игра окончена, дружба сохранена! 🤝",
        "До новых встреч в тёмных переулках! 🌃"
    ]
    outro = random.choice(phrases)
    
    # Красивое оформление отчёта
    header = "🏆═════════════════════════════════════🏆"
    footer = "🏆═════════════════════════════════════🏆"
    
    rep = (
        f"{header}\n"
        f"🎭 ИТОГИ ПАРТИИ №{game.game_number} 🎭\n"
        f"{header}\n"
        f"{'\n'.join(all_players)}\n"
        f"\n📢 {win_text}\n"
        f"⭐ {mvp_text}\n"
        f"\n{outro}\n"
        f"{footer}"
    )
    
    await bot.send_message(game.chat_id, rep)

# Хэндлеры команд
@dp.message(Command("newgame"), is_group)
async def cmd_newgame(message: Message):
    chat_id = message.chat.id
    game = game_manager.get(chat_id)
    if game and game.phase != "ended":
        await message.reply("Игра уже идёт! Сначала завершите текущую партию.")
        return
    
    game = game_manager.new_game(chat_id)
    game.game_number = stats_manager.next_game_number(chat_id)
    await send_chat(game, "Новая игра мафия! Регистрация открыта. Присоединяйся командой /join")
    await send_random_sticker(game)

@dp.message(Command("join"), is_group)
async def cmd_join(message: Message):
    chat_id = message.chat.id
    game = game_manager.get(chat_id)
    if not game or game.phase != "registration":
        await message.reply("Регистрация закрыта!")
        return
    
    user_id = message.from_user.id
    name = message.from_user.full_name or f"Player_{user_id}"
    
    if user_id in game.players:
        await message.reply("Вы уже в игре!")
        return
    
    game.players[user_id] = name
    game.alive.add(user_id)
    await message.reply(f"{name} присоединился к игре! Игроков: {len(game.players)}")
    
    if len(game.players) >= MIN_PLAYERS and not game.started:
        game.started = True
        await send_chat(game, "Минимальное количество игроков набрано! Начните игру командой /startgame")

@dp.message(Command("leave"), is_group)
async def cmd_leave(message: Message):
    chat_id = message.chat.id
    game = game_manager.get(chat_id)
    if not game or game.phase != "registration":
        await message.reply("Нельзя выйти на этом этапе!")
        return
    
    user_id = message.from_user.id
    if user_id not in game.players:
        await message.reply("Вы не в игре!")
        return
    
    name = game.players[user_id]
    del game.players[user_id]
    game.alive.discard(user_id)
    await message.reply(f"{name} вышел из игры. Игроков: {len(game.players)}")

@dp.message(Command("startgame"), is_group)
async def cmd_startgame(message: Message):
    chat_id = message.chat.id
    game = game_manager.get(chat_id)
    if not game or game.phase != "registration":
        await message.reply("Игра не на этапе регистрации!")
        return
    
    if len(game.players) < MIN_PLAYERS:
        await message.reply(f"Минимум {MIN_PLAYERS} игрока для старта!")
        return
    
    await start_game(game)

@dp.message(Command("status"), is_group)
async def cmd_status(message: Message):
    chat_id = message.chat.id
    game = game_manager.get(chat_id)
    if not game:
        await message.reply("Игра не найдена.")
        return
    
    status_text = build_status_text(game)
    await message.reply(status_text)

@dp.message(Command("stats"), is_group)
async def cmd_stats(message: Message):
    chat_id = message.chat.id
    stats_text = stats_manager.get_stats_text(chat_id)
    await message.reply(stats_text)

@dp.message(Command("forceend"), is_group)
async def cmd_forceend(message: Message):
    chat_id = message.chat.id
    game = game_manager.get(chat_id)
    if not game:
        await message.reply("Игра не найдена.")
        return
    
    await end_game(game, "Игра принудительно завершена администратором.")

# Вспомогательные функции игры
async def start_game(game: Game):
    game.phase = "night"
    game.started = True
    
    # Распределяем роли
    player_ids = list(game.players.keys())
    random.shuffle(player_ids)
    
    # 1 мафия, 1 доктор, 1 комиссар, остальные мирные
    roles = ["Мафия", "Доктор", "Комиссар"] + ["Мирный"] * (len(player_ids) - 3)
    random.shuffle(roles)
    
    for i, player_id in enumerate(player_ids):
        game.roles[player_id] = roles[i]
        # Инициализируем счётчик убийств для мафии
        if roles[i] == "Мафия":
            game.mafia_kills[player_id] = 0
    
    await send_chat(game, "Игра началась! Ночь опускается на город...")
    await send_random_sticker(game)
    
    # Отправляем роли игрокам в личные сообщения
    for player_id, role in game.roles.items():
        try:
            emoji = ROLE_EMOJI.get(role, "")
            await bot.send_message(player_id, f"{emoji} Ваша роль: {role}")
            
            if role == "Мафия" and player_id in game.alive:
                await send_mafia_action(game, player_id)
            elif role == "Доктор" and player_id in game.alive:
                await send_doctor_action(game, player_id)
            elif role == "Комиссар" and player_id in game.alive:
                await send_commissar_action(game, player_id)
        except Exception:
            await send_chat(game, f"⚠️ {game.players.get(player_id)} не получил роль! Проверьте настройки личных сообщений.")

async def send_mafia_action(game: Game, mafia_id: int):
    kb = build_player_kb(game, "mafia")
    try:
        await bot.send_message(
            mafia_id,
            "😈 Выберите жертву на tonight:",
            reply_markup=kb.as_markup()
        )
    except Exception:
        pass

async def send_doctor_action(game: Game, doctor_id: int):
    kb = build_player_kb(game, "doctor")
    try:
        await bot.send_message(
            doctor_id,
            "🩺 Кого вылечить tonight?",
            reply_markup=kb.as_markup()
        )
    except Exception:
        pass

async def send_commissar_action(game: Game, commissar_id: int):
    kb = build_player_kb(game, "commissar")
    try:
        await bot.send_message(
            commissar_id,
            "🕵️ Кого проверить tonight?",
            reply_markup=kb.as_markup()
        )
    except Exception:
        pass

# Хэндлеры колбэков
@dp.callback_query(F.data.startswith("mafia:"))
async def callback_mafia(callback: CallbackQuery):
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    target_id = int(parts[2])
    
    game = game_manager.get(chat_id)
    if not game or game.phase != "night":
        await callback.answer("Не время для мафии!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    if game.roles.get(user_id) != "Мафия" or user_id not in game.alive:
        await callback.answer("Вы не мафия!", show_alert=True)
        return
    
    if target_id not in game.alive:
        await callback.answer("Цель уже мертва!", show_alert=True)
        return
    
    game.mafia_target = target_id
    await callback.answer(f"Выбрано: {game.players.get(target_id)}")
    await callback.message.edit_text("😈 Ваш выбор сделан. Ожидайте утра...")
    
    await check_night_complete(game)

@dp.callback_query(F.data.startswith("doctor:"))
async def callback_doctor(callback: CallbackQuery):
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    target_id = int(parts[2])
    
    game = game_manager.get(chat_id)
    if not game or game.phase != "night":
        await callback.answer("Не время для доктора!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    if game.roles.get(user_id) != "Доктор" or user_id not in game.alive:
        await callback.answer("Вы не доктор!", show_alert=True)
        return
    
    game.doctor_target = target_id
    await callback.answer(f"Выбрано: {game.players.get(target_id)}")
    await callback.message.edit_text("🩺 Ваш выбор сделан. Ожидайте утра...")
    
    await check_night_complete(game)

@dp.callback_query(F.data.startswith("commissar:"))
async def callback_commissar(callback: CallbackQuery):
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    target_id = int(parts[2])
    
    game = game_manager.get(chat_id)
    if not game or game.phase != "night":
        await callback.answer("Не время для комиссара!", show_alert=True)
        return
    
    user_id = callback.from_user.id
    if game.roles.get(user_id) != "Комиссар" or user_id not in game.alive:
        await callback.answer("Вы не комиссар!", show_alert=True)
        return
    
    target_role = game.roles.get(target_id, "неизвестно")
    emoji = ROLE_EMOJI.get(target_role, "")
    
    await callback.answer(f"{game.players.get(target_id)} — {emoji} {target_role}")
    await callback.message.edit_text(f"🕵️ {game.players.get(target_id)} — {emoji} {target_role}")
    
    game.commissar_target = target_id
    await check_night_complete(game)

async def check_night_complete(game: Game):
    # Проверяем, все ли ночные действия сделаны
    mafia_alive = any(role == "Мафия" and uid in game.alive for uid, role in game.roles.items())
    doctor_alive = any(role == "Доктор" and uid in game.alive for uid, role in game.roles.items())
    commissar_alive = any(role == "Комиссар" and uid in game.alive for uid, role in game.roles.items())
    
    ready = True
    if mafia_alive and game.mafia_target is None:
        ready = False
    if doctor_alive and game.doctor_target is None:
        ready = False
    if commissar_alive and game.commissar_target is None:
        ready = False
    
    if ready:
        await process_night(game)

async def process_night(game: Game):
    # Обрабатываем результаты ночи
    killed = game.mafia_target
    healed = game.doctor_target
    
    if killed and killed != healed:
        killed_name = game.players.get(killed)
        killed_role = game.roles.get(killed, "неизвестно")
        role_emoji = ROLE_EMOJI.get(killed_role, "")
        
        game.alive.remove(killed)
        # Увеличиваем счётчик убийств для мафии
        for mafia_id, role in game.roles.items():
            if role == "Мафия" and mafia_id in game.alive:
                game.mafia_kills[mafia_id] = game.mafia_kills.get(mafia_id, 0) + 1
                break  # Учитываем только одно убийство за ночь
        
        # Красивое сообщение об убийстве с раскрытием роли
        night_message = (
            f"☠️ Ночью был убит {killed_name}!\n"
            f"🎭 Его роль была: {role_emoji} {killed_role}"
        )
        
        await send_chat(game, night_message)
        await send_random_sticker(game)
    elif killed and killed == healed:
        await send_chat(game, "🩺 Доктор спас жертву tonight! Никто не умер.")
    else:
        await send_chat(game, "🌙 Ночь прошла спокойно. Никто не умер.")
    
    # Проверяем победу
    victory = check_victory(game)
    if victory:
        await end_game(game, victory)
        return
    
    # Начинаем день
    game.phase = "day"
    game.reset_night_actions()
    await send_chat(game, "☀️ Наступает день! Время голосования.")
    await start_voting(game)

async def start_voting(game: Game):
    game.reset_day_votes()
    kb = build_player_kb(game, "vote", include_abstain=True)
    await bot.send_message(
        game.chat_id,
        "⚖️ Голосование! Кого исключаем из города?",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data.startswith("vote:"))
async def callback_vote(callback: CallbackQuery):
    parts = callback.data.split(":")
    chat_id = int(parts[1])
    target_id = int(parts[2])
    
    game = game_manager.get(chat_id)
    if not game or game.phase != "day":
        await callback.answer("Не время для голосования!", show_alert=True)
        return
    
    voter_id = callback.from_user.id
    if voter_id not in game.alive:
        await callback.answer("Мёртвые не голосуют!", show_alert=True)
        return
    
    game.day_votes[voter_id] = target_id
    await callback.answer(f"Вы проголосовали за {game.players.get(target_id) if target_id != -1 else 'воздержался'}")
    
    # Проверяем, все ли проголосовали
    if len(game.day_votes) == len(game.alive):
        await process_voting(game)

async def process_voting(game: Game):
    # Подсчитываем голоса
    votes = {}
    for target_id in game.day_votes.values():
        if target_id != -1:  # Не считаем воздержавшихся
            votes[target_id] = votes.get(target_id, 0) + 1
    
    if votes:
        # Находим игрока с максимальным количеством голосов
        max_votes = max(votes.values())
        eliminated = [uid for uid, count in votes.items() if count == max_votes][0]
        
        eliminated_name = game.players.get(eliminated)
        eliminated_role = game.roles.get(eliminated, "неизвестно")
        role_emoji = ROLE_EMOJI.get(eliminated_role, "")
        
        game.alive.remove(eliminated)
        
        # Красивое сообщение об исключении с раскрытием роли
        exclusion_message = (
            f"⚖️ Голосование завершено!\n"
            f"🔪 Исключён: {eliminated_name} ({max_votes} {'голос' if max_votes == 1 else 'голоса' if max_votes < 5 else 'голосов'})\n"
            f"🎭 Его роль была: {role_emoji} {eliminated_role}"
        )
        
        await send_chat(game, exclusion_message)
        
        # Добавляем случайный стикер для драмы
        await send_random_sticker(game)
    else:
        await send_chat(game, "⚖️ Все воздержались! Никто не исключён.")
    
    # Проверяем победу
    victory = check_victory(game)
    if victory:
        await end_game(game, victory)
        return
    
    # Начинаем новую ночь
    game.phase = "night"
    game.reset_day_votes()
    await send_chat(game, "🌙 Наступает ночь...")
    await send_random_sticker(game)
    
    # Отправляем ночные действия
    for player_id, role in game.roles.items():
        if player_id in game.alive:
            if role == "Мафия":
                await send_mafia_action(game, player_id)
            elif role == "Доктор":
                await send_doctor_action(game, player_id)
            elif role == "Комиссар":
                await send_commissar_action(game, player_id)

async def end_game(game: Game, reason: str):
    # Записываем статистику
    winners = set()
    if "мафии" in reason.lower():
        # Победа мафии
        for uid, role in game.roles.items():
            if role == "Мафия":
                winners.add(uid)
    else:
        # Победа мирных
        for uid, role in game.roles.items():
            if role != "Мафия":
                winners.add(uid)
    
    stats_manager.record_result(game.chat_id, game.players, winners, game.roles)
    
    # Отправляем отчёт
    await send_game_report(game, reason)
    
    # Завершаем игру
    game_manager.end_game(game.chat_id)

async def main():
    load_dotenv()
    global bot
    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        print("BOT_TOKEN не найден!")
        return
    
    bot = Bot(token=bot_token)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
