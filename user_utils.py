
async def resolve_users_map(interaction, keys):
    user_map = {}
    unique_ids = set(k.get('discord_id') for k in keys if k.get('discord_id'))
    
    for uid in unique_ids:
        try:
            uid_int = int(uid)
            # 1. Try Guild Cache
            member = interaction.guild.get_member(uid_int)
            if member:
                user_map[uid] = member.name
                continue
                
            # 2. Try Bot Global Cache
            user = interaction.client.get_user(uid_int)
            if user:
                user_map[uid] = user.name
                continue
                
            # 3. API Fetch (Slower but reliable)
            user = await interaction.client.fetch_user(uid_int)
            user_map[uid] = user.name
            
        except Exception:
            user_map[uid] = uid # Fallback to ID
            
    return user_map
