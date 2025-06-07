import os
import csv
import discord
import aiohttp
import asyncio
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from itertools import combinations
import unicodedata

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

BONUS_POINT = 20
DEDUCATION_POINT = -20
CSV_FILE = "member_data.csv"

# ---- CSV 操作 ----

def load_bonus_points():
    if not os.path.exists(CSV_FILE):
        return {}
    with open(CSV_FILE, newline='', encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)
        return {rows[0]: int(rows[1]) for rows in reader}

def save_bonus_points(bonus_dict):
    with open(CSV_FILE, 'w', newline='', encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        for name, point in bonus_dict.items():
            writer.writerow([name, point])

# ---- API処理部 ----

async def get_puuid(session, name, tag):
    url = f"https://asia.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{name}/{tag}"
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with session.get(url, headers=headers) as resp:
        data = await resp.json()
        return data

async def get_summoner_and_rank_info(session, puuid):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    url = f"https://jp1.api.riotgames.com/lol/summoner/v4/summoners/by-puuid/{puuid}"
    async with session.get(url, headers=headers) as resp:
        summoner = await resp.json()
    
    level = summoner.get("summonerLevel", 0)
    summoner_id = summoner.get("id")

    url2 = f"https://jp1.api.riotgames.com/lol/league/v4/entries/by-summoner/{summoner_id}"
    async with session.get(url2, headers=headers) as resp:
        ranks = await resp.json()
        for r in ranks:
            if r["queueType"] == "RANKED_SOLO_5x5":
                return level, r
    return level, None

async def get_recent_kda(session, puuid):
    headers = {"X-Riot-Token": RIOT_API_KEY}
    ids_url = f"https://asia.api.riotgames.com/lol/match/v5/matches/by-puuid/{puuid}/ids?start=0&count=5"
    async with session.get(ids_url, headers=headers) as resp:
        match_ids = await resp.json()

    if not isinstance(match_ids, list) or len(match_ids) == 0:
        return 0.0

    total_k = total_a = total_d = 0

    for match_id in match_ids:
        match_url = f"https://asia.api.riotgames.com/lol/match/v5/matches/{match_id}"
        async with session.get(match_url, headers=headers) as resp:
            match_detail = await resp.json()

        participants = match_detail.get("info", {}).get("participants", [])
        for p in participants:
            if p.get("puuid") == puuid:
                total_k += p.get("kills", 0)
                total_a += p.get("assists", 0)
                total_d += p.get("deaths", 0)
                break

        await asyncio.sleep(0.5)

    if total_d == 0:
        avg_kda = (total_k + total_a) / 1
    else:
        avg_kda = (total_k + total_a) / total_d

    return round(avg_kda, 2)

def calculate_score(rank_info, level, avg_kda):
    tier_weight = {
        "IRON": 1, "BRONZE": 2, "SILVER": 3, "GOLD": 4,
        "PLATINUM": 5, "EMERALD": 6, "DIAMOND": 7,
        "MASTER": 8, "GRANDMASTER": 9, "CHALLENGER": 10
    }

    if rank_info is None:
        base = 200
        winrate_bonus = division_bonus = 0
    else:
        tier = rank_info.get("tier", "")
        division = rank_info.get("rank", "")
        wins = rank_info.get("wins", 10)
        losses = rank_info.get("losses", 10)
        winrate = wins / max(wins + losses, 1)

        base = tier_weight.get(tier, 2) * 100
        division_bonus = {"IV": 20, "III": 40, "II": 60, "I": 80}.get(division, 20)
        winrate_bonus = int(winrate * 10)

    level_bonus = int(level * 0.7)
    kda_bonus = int(avg_kda * 25)

    return base + division_bonus + winrate_bonus + level_bonus + kda_bonus

def divide_teams(players):
    min_diff = float('inf')
    best_team1 = best_team2 = []

    indices = list(range(10))
    for combo in combinations(indices, 5):
        team1 = [players[i] for i in combo]
        team2 = [players[i] for i in indices if i not in combo]

        score1 = sum(p["score"] for p in team1)
        score2 = sum(p["score"] for p in team2)
        diff = abs(score1 - score2)

        if diff < min_diff:
            min_diff = diff
            best_team1, best_team2 = team1, team2

    return best_team1, best_team2

# ---- Discordコマンド ----

@tree.command(name="member", description="10人分のSummonerName#Tagを空白区切りで入力して、バランスよく2チームに分けます。")
@app_commands.describe(
    list="例: Faker#KR1 Chovy#KR2 Knight#CN1 ... のように空白区切りで10人入力"
)
async def member(interaction: discord.Interaction, list: str):
    await interaction.response.defer()

    entries = list.strip().split()
    if len(entries) != 10:
        await interaction.followup.send("⚠ 10人分のSummonerName#Tagを空白区切りで入力してください。")
        return

    bonus_points = load_bonus_points()
    player_data = []

    async with aiohttp.ClientSession() as session:
        for entry in entries:
            try:
                name, tag = entry.split("#")
                account = await get_puuid(session, name, tag)
                puuid = account.get("puuid")
                if puuid is None:
                    raise ValueError("puuid が取得できませんでした。")

                level, rank_info = await get_summoner_and_rank_info(session, puuid)
                avg_kda = await get_recent_kda(session, puuid)
                score = calculate_score(rank_info, level, avg_kda)
                bonus = bonus_points.get(f"{name}#{tag}", 0)
                score += bonus

                winrate = 0
                if rank_info:
                    wins = rank_info.get("wins", 0)
                    losses = rank_info.get("losses", 0)
                    total_games = wins + losses
                    winrate = round((wins / total_games) * 100, 1) if total_games > 0 else 0

                player_data.append({
                    "name": f"{name}#{tag}",
                    "score": score,
                    "rank": (rank_info["tier"] + " " + rank_info["rank"]) if rank_info else "UNRANKED",
                    "level": level,
                    "avg_kda": avg_kda,
                    "winrate": winrate
                })

                await asyncio.sleep(1)
            except Exception as e:
                await interaction.followup.send(f"❌ データ取得エラー: `{entry}`\n```{e}```")
                return

    team1, team2 = divide_teams(player_data)

    total1 = sum(p["score"] for p in team1)
    total2 = sum(p["score"] for p in team2)

    bot.latest_teams = {"team1": team1, "team2": team2}

    result = (
    f"**✅ チーム分け結果**\n\n"
    f"**🏆 チームA（合計 {total1}pt）**\n```{format_team(team1)}```\n"
    f"**🔥 チームB（合計 {total2}pt）**\n```{format_team(team2)}```"
)

    await interaction.followup.send(result)

def get_display_width(text):
    """全角文字は2幅として表示幅を返す"""
    return sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)

def pad_display(text, total_width):
    """表示幅に基づいて右側にスペースを足してパディング"""
    pad = total_width - get_display_width(text)
    return text + ' ' * max(0, pad)

def format_team(team):
    name_width = 26
    rank_width = 15
    level_width = 6
    kda_width = 6
    wr_width = 7
    score_width = 6

    header = (
        pad_display('Name', name_width) + " " +
        pad_display('Rank', rank_width) + " " +
        pad_display('Lv.', level_width) + " " +
        pad_display('KDA', kda_width) + " " +
        pad_display('WR', wr_width) + " " +
        pad_display('Score', score_width)
    )
    separator = "-" * get_display_width(header)
    lines = [header, separator]

    for p in team:
        name = pad_display(p['name'], name_width)
        rank = pad_display(p['rank'], rank_width)
        level = pad_display(f"Lv.{p['level']}", level_width)
        kda = pad_display(str(p['avg_kda']), kda_width)
        wr = pad_display(f"{p['winrate']}%", wr_width)
        score = pad_display(f"{p['score']}pt", score_width)
        lines.append(f"{name} {rank} {level} {kda} {wr} {score}")

    return "\n".join(lines)

@tree.command(name="win", description="勝利チーム（AまたはB）を記録し、ボーナスを付与します")
@app_commands.describe(team="勝利チームを A または B で指定")
async def win(interaction: discord.Interaction, team: str):
    team = team.upper()
    if team not in {"A", "B"}:
        await interaction.response.send_message("⚠ チームは A または B を指定してください。", ephemeral=True)
        return

    if not hasattr(bot, "latest_teams"):
        await interaction.response.send_message("❌ `/member` 実行後に `/win` を使ってください。", ephemeral=True)
        return

    team_key = "team1" if team == "A" else "team2"
    losing_key = "team2" if team == "A" else "team1"
    winning_team = bot.latest_teams.get(team_key, [])
    losing_team = bot.latest_teams.get(losing_key, [])

    bonus_dict = load_bonus_points()
    for player in winning_team:
        name = player["name"]
        bonus_dict[name] = bonus_dict.get(name, 0) + BONUS_POINT

    for player in losing_team:
        name = player["name"]
        bonus_dict[name] = bonus_dict.get(name, 0) + DEDUCATION_POINT

    save_bonus_points(bonus_dict)
    await interaction.response.send_message(
        f"✅ チーム{team}の勝利を記録しました。\n"
        f"🏆 勝利チーム: +{BONUS_POINT}pt\n"
        f"💀 敗北チーム: {DEDUCATION_POINT}pt"
    )

@tree.command(name="show_bonus", description="現在のボーナスポイント一覧を表示します")
async def show_bonus(interaction: discord.Interaction):
    bonus_dict = load_bonus_points()
    if not bonus_dict:
        await interaction.response.send_message("📄 ボーナスポイントはまだ記録されていません。")
        return

    msg = "**📈 現在のボーナスポイント一覧：**\n"
    for name, point in bonus_dict.items():
        msg += f"- {name}: {point}pt\n"
    await interaction.response.send_message(msg)

@tree.command(name="reset_bonus", description="ボーナスポイント記録をすべてリセットします（要注意）")
async def reset_bonus(interaction: discord.Interaction):
    save_bonus_points({})
    await interaction.response.send_message("🗑️ ボーナスポイントをすべてリセットしました。")

@tree.command(name="help", description="Botの使い方を表示します")
async def help_command(interaction: discord.Interaction):
    msg = (
        "**LOLチーム分けBotの使い方**\n\n"
        "1. `/member` で 10人のサモナー名を空白区切りで入力\n"
        "2. 自動で2チームに分けて表示\n"
        "3. 勝利したチームを `/win A` または `/win B` で登録（ボーナスポイント加算）\n"
        "4. `/show_bonus` でボーナス状況確認、`/reset_bonus` で全リセット\n"
        "\n※ Riot API によるレート制限があります。連続実行は時間を空けてください。"
    )
    await interaction.response.send_message(msg)

@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")

bot.run(TOKEN)
