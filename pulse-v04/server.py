import re, os, json, secrets, hashlib, hmac, mimetypes, time, threading
from collections import defaultdict, deque
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from http.cookies import SimpleCookie
import psycopg
from psycopg.rows import dict_row
from psycopg.errors import UniqueViolation
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('PULSE_DATA_DIR',str(ROOT))).resolve(); DATA.mkdir(parents=True,exist_ok=True)
DATABASE_URL=os.environ.get('DATABASE_URL','').strip()
if not DATABASE_URL: raise RuntimeError('DATABASE_URL is required; set it in Render Environment')
UP=DATA/'uploads'; UP.mkdir(exist_ok=True)
ADMIN_ID=os.environ.get('PULSE_ADMIN_USER_ID','').strip()
RATE=defaultdict(deque); RATE_LOCK=threading.Lock()
MAX=12*1024*1024

def db():
 return psycopg.connect(DATABASE_URL, row_factory=dict_row)

SCHEMA = [
 "CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,bio TEXT DEFAULT '',password BYTEA NOT NULL,salt BYTEA NOT NULL,created BIGINT NOT NULL,verified INTEGER NOT NULL DEFAULT 0,banned INTEGER NOT NULL DEFAULT 0)",
 "CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id),expires BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS posts(id BIGSERIAL PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(id),body TEXT NOT NULL,media TEXT DEFAULT '',kind TEXT DEFAULT 'post',created BIGINT NOT NULL,pinned INTEGER NOT NULL DEFAULT 0)",
 "CREATE TABLE IF NOT EXISTS likes(user_id BIGINT REFERENCES users(id),post_id BIGINT REFERENCES posts(id) ON DELETE CASCADE,PRIMARY KEY(user_id,post_id))",
 "CREATE TABLE IF NOT EXISTS comments(id BIGSERIAL PRIMARY KEY,post_id BIGINT REFERENCES posts(id) ON DELETE CASCADE,user_id BIGINT REFERENCES users(id),body TEXT NOT NULL,created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS follows(follower BIGINT REFERENCES users(id),followed BIGINT REFERENCES users(id),PRIMARY KEY(follower,followed))",
 "CREATE TABLE IF NOT EXISTS messages(id BIGSERIAL PRIMARY KEY,sender BIGINT REFERENCES users(id),recipient BIGINT REFERENCES users(id),body TEXT NOT NULL,created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS bookmarks(user_id BIGINT REFERENCES users(id),post_id BIGINT REFERENCES posts(id) ON DELETE CASCADE,PRIMARY KEY(user_id,post_id))",
 "CREATE TABLE IF NOT EXISTS stories(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id),body TEXT NOT NULL DEFAULT '',media TEXT NOT NULL DEFAULT '',created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS communities(id BIGSERIAL PRIMARY KEY,name TEXT UNIQUE NOT NULL,description TEXT NOT NULL,owner BIGINT REFERENCES users(id),created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS community_members(community_id BIGINT REFERENCES communities(id) ON DELETE CASCADE,user_id BIGINT REFERENCES users(id),PRIMARY KEY(community_id,user_id))",
 "CREATE TABLE IF NOT EXISTS community_posts(id BIGSERIAL PRIMARY KEY,community_id BIGINT REFERENCES communities(id) ON DELETE CASCADE,user_id BIGINT REFERENCES users(id),body TEXT NOT NULL,created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS notifications(id BIGSERIAL PRIMARY KEY,recipient BIGINT REFERENCES users(id),actor BIGINT REFERENCES users(id),kind TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',created BIGINT NOT NULL,seen INTEGER NOT NULL DEFAULT 0)",
 "CREATE TABLE IF NOT EXISTS reports(id BIGSERIAL PRIMARY KEY,reporter_id BIGINT NOT NULL REFERENCES users(id),post_id BIGINT REFERENCES posts(id) ON DELETE SET NULL,reason TEXT NOT NULL,resolved INTEGER NOT NULL DEFAULT 0,created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS chat_groups(id BIGSERIAL PRIMARY KEY,name TEXT NOT NULL,owner BIGINT NOT NULL REFERENCES users(id),created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS chat_members(group_id BIGINT NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id),PRIMARY KEY(group_id,user_id))",
 "CREATE TABLE IF NOT EXISTS chat_messages(id BIGSERIAL PRIMARY KEY,group_id BIGINT NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id),body TEXT NOT NULL,created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS voice_signals(id BIGSERIAL PRIMARY KEY,group_id BIGINT NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,sender BIGINT NOT NULL REFERENCES users(id),recipient BIGINT NOT NULL REFERENCES users(id),payload TEXT NOT NULL,created BIGINT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS voice_presence(group_id BIGINT NOT NULL REFERENCES chat_groups(id) ON DELETE CASCADE,user_id BIGINT NOT NULL REFERENCES users(id),updated BIGINT NOT NULL,PRIMARY KEY(group_id,user_id))",
 "CREATE TABLE IF NOT EXISTS site_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)",
 "CREATE TABLE IF NOT EXISTS media_files(name TEXT PRIMARY KEY, owner_id BIGINT REFERENCES users(id) ON DELETE SET NULL, mime TEXT NOT NULL, content BYTEA NOT NULL, created BIGINT NOT NULL)",
 "CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created DESC)",
 "CREATE INDEX IF NOT EXISTS idx_msg ON messages(sender,recipient,created)",
 "CREATE INDEX IF NOT EXISTS idx_notifications ON notifications(recipient,id DESC)",
 "CREATE INDEX IF NOT EXISTS idx_stories ON stories(created DESC)",
]

def init_db():
 with db() as c:
  for statement in SCHEMA: c.execute(statement)
  # Idempotent additions for existing Neon installations.
  c.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS verified INTEGER NOT NULL DEFAULT 0')
  c.execute('ALTER TABLE users ADD COLUMN IF NOT EXISTS banned INTEGER NOT NULL DEFAULT 0')
  c.execute('ALTER TABLE posts ADD COLUMN IF NOT EXISTS pinned INTEGER NOT NULL DEFAULT 0')
  c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar TEXT NOT NULL DEFAULT ''")
  c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS banner TEXT NOT NULL DEFAULT ''")
  c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS website TEXT NOT NULL DEFAULT ''")
  c.execute("ALTER TABLE posts ADD COLUMN IF NOT EXISTS request_id TEXT")
  c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_posts_request_id ON posts(user_id,request_id) WHERE request_id IS NOT NULL")

def migrate_legacy_media():
 # Only files still present on the running host can be recovered.
 with db() as c:
  for file in UP.iterdir():
   if not file.is_file() or not re.fullmatch(r'[a-f0-9]{36}\.(?:png|jpg|webp|gif|mp4|webm)',file.name):continue
   if file.stat().st_size>MAX:continue
   mime=mimetypes.guess_type(file.name)[0] or 'application/octet-stream'
   c.execute('INSERT INTO media_files(name,owner_id,mime,content,created) VALUES(%s,NULL,%s,%s,%s) ON CONFLICT(name) DO NOTHING',(file.name,mime,file.read_bytes(),now()))

def is_admin(row):
 return row is not None and bool(ADMIN_ID) and str(row['id']) == ADMIN_ID

def notify(c,recipient,actor,kind,detail=''):
 if recipient!=actor:c.execute('INSERT INTO notifications(recipient,actor,kind,detail,created) VALUES(%s,%s,%s,%s,%s)',(recipient,actor,kind,str(detail)[:100],now()))
def now():return int(time.time())
def public_user(row):
    keys = row.keys()
    return dict(
        id=row['id'],
        username=row['username'],
        name=row['name'],
        bio=row['bio'],
        avatar=row.get('avatar',''),
        banner=row.get('banner',''),
        website=row.get('website',''),
        created=row['created'],
        verified=bool(row['verified']) if 'verified' in keys else False,
        admin=is_admin(row)
    )
class App(BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def send(self,code,data,headers=None):
  raw=json.dumps(data).encode();self.send_response(code);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Content-Length',str(len(raw)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store');
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(raw)
 def fail(self,msg,code=400):self.send(code,{'error':msg})
 def identity(self,c):
  cookie=SimpleCookie()
  try:cookie.load(self.headers.get('Cookie',''))
  except Exception:return None
  token=cookie.get('pulse_session');token=token.value if token else ''
  return c.execute('SELECT users.* FROM users JOIN sessions ON sessions.user_id=users.id WHERE sessions.token=%s AND sessions.expires>%s',(token,now())).fetchone()
 def body(self):
  size=int(self.headers.get('Content-Length','0'))
  if size>MAX+1024:return None
  return self.rfile.read(size)
 def do_GET(self):
  path=urlparse(self.path).path
  if path.startswith('/uploads/'):
   name=path[len('/uploads/'):]
   if not re.fullmatch(r'[a-f0-9]{36}\.(?:png|jpg|webp|gif|mp4|webm)',name):return self.send_error(404)
   with db() as c:
    item=c.execute('SELECT mime,content FROM media_files WHERE name=%s',(name,)).fetchone()
   if item:
    data=bytes(item['content']);self.send_response(200)
    self.send_header('Content-Type',item['mime'])
    self.send_header('Content-Length',str(len(data)))
    self.send_header('X-Content-Type-Options','nosniff')
    self.send_header('Cache-Control','public, max-age=86400')
    self.end_headers();self.wfile.write(data);return
   # Legacy uploads may still be available until Render's next restart.
   legacy=UP/name
   if legacy.is_file():
    data=legacy.read_bytes();self.send_response(200)
    self.send_header('Content-Type',mimetypes.guess_type(name)[0] or 'application/octet-stream')
    self.send_header('Content-Length',str(len(data)))
    self.send_header('X-Content-Type-Options','nosniff')
    self.end_headers();self.wfile.write(data);return
   return self.send_error(404)
  if path=='/' or path.startswith('/static/'):
   target=(ROOT/'static'/'index.html' if path=='/' else ROOT/path.lstrip('/')).resolve()
   if not target.is_relative_to((ROOT/'static').resolve()) or not target.is_file():return self.send_error(404)
   mime=mimetypes.guess_type(str(target))[0] or 'application/octet-stream';data=target.read_bytes();self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data);return
  with db() as c:
   me=self.identity(c)
   if path=='/api/me':return self.send(200,{'user':public_user(me) if me else None})
   if not me:return self.fail('Log in first',401)
   q=parse_qs(urlparse(self.path).query)
   if path=='/api/site-settings':
    rows=c.execute('SELECT key,value FROM site_settings').fetchall()
    return self.send(200,{'settings':{r['key']:r['value'] for r in rows}})
   if path=='/api/group-list':
    rows=c.execute('SELECT g.*, (SELECT COUNT(*) FROM chat_members m WHERE m.group_id=g.id) AS members FROM chat_groups g JOIN chat_members mine ON mine.group_id=g.id AND mine.user_id=%s ORDER BY g.id DESC',(me['id'],)).fetchall()
    return self.send(200,{'groups':rows})
   if path=='/api/group-detail':
    try:gid=int(q.get('id',['0'])[0])
    except ValueError:return self.fail('Invalid group')
    if not c.execute('SELECT 1 FROM chat_members WHERE group_id=%s AND user_id=%s',(gid,me['id'])).fetchone():return self.fail('Not a member',403)
    group=c.execute('SELECT * FROM chat_groups WHERE id=%s',(gid,)).fetchone()
    members=c.execute('SELECT u.id,u.username,u.name,u.avatar FROM chat_members m JOIN users u ON u.id=m.user_id WHERE m.group_id=%s ORDER BY u.username',(gid,)).fetchall()
    messages=c.execute('SELECT m.id,m.body,m.created,m.user_id,u.username,u.name FROM chat_messages m JOIN users u ON u.id=m.user_id WHERE m.group_id=%s ORDER BY m.id DESC LIMIT 100',(gid,)).fetchall()
    voice=c.execute('SELECT u.id,u.username FROM voice_presence v JOIN users u ON u.id=v.user_id WHERE v.group_id=%s AND v.updated>%s',(gid,now()-35)).fetchall()
    return self.send(200,{'group':group,'members':members,'messages':list(reversed(messages)),'voice':voice})
   if path=='/api/voice-signals':
    try:gid=int(q.get('group',['0'])[0]);after=int(q.get('after',['0'])[0])
    except ValueError:return self.fail('Invalid query')
    if not c.execute('SELECT 1 FROM chat_members WHERE group_id=%s AND user_id=%s',(gid,me['id'])).fetchone():return self.fail('Not a member',403)
    rows=c.execute('SELECT id,sender,payload FROM voice_signals WHERE group_id=%s AND recipient=%s AND id>%s AND created>%s ORDER BY id LIMIT 100',(gid,me['id'],after,now()-120)).fetchall()
    return self.send(200,{'signals':rows})
   if path=='/api/admin/overview':
    if not is_admin(me):return self.fail('Admin only',403)
    counts={table:c.execute('SELECT COUNT(*) AS n FROM '+table).fetchone()['n'] for table in ('users','posts','reports')}
    reports=c.execute('SELECT r.*,u.username AS reporter_name,p.body AS post_body FROM reports r JOIN users u ON u.id=r.reporter_id LEFT JOIN posts p ON p.id=r.post_id WHERE r.resolved=0 ORDER BY r.id DESC LIMIT 100').fetchall()
    people=c.execute('SELECT id,username,name,verified,banned FROM users ORDER BY id DESC LIMIT 100').fetchall()
    return self.send(200,{'counts':counts,'reports':reports,'users':people})
   if path=='/api/profile-view':
    username=q.get('username',[''])[0].strip().lower()
    if not username:return self.fail('Username required')
    u=c.execute('SELECT * FROM users WHERE username=%s',(username,)).fetchone()
    if not u:return self.fail('User not found',404)
    uid=u['id']
    stats=c.execute('SELECT (SELECT COUNT(*) FROM posts WHERE user_id=%s) AS posts,(SELECT COUNT(*) FROM follows WHERE followed=%s) AS followers,(SELECT COUNT(*) FROM follows WHERE follower=%s) AS following',(uid,uid,uid)).fetchone()
    follow=bool(c.execute('SELECT 1 FROM follows WHERE follower=%s AND followed=%s',(me['id'],uid)).fetchone())
    rows=c.execute("""SELECT p.*,u.username,u.name,u.bio,u.verified,u.avatar,p.pinned,(SELECT COUNT(*) FROM likes WHERE post_id=p.id) likes,(SELECT COUNT(*) FROM comments WHERE post_id=p.id) comments,EXISTS(SELECT 1 FROM likes WHERE post_id=p.id AND user_id=%s) liked,EXISTS(SELECT 1 FROM follows WHERE follower=%s AND followed=p.user_id) following,EXISTS(SELECT 1 FROM bookmarks WHERE user_id=%s AND post_id=p.id) bookmarked FROM posts p JOIN users u ON u.id=p.user_id WHERE p.user_id=%s ORDER BY p.pinned DESC,p.created DESC,p.id DESC LIMIT 100""",(me['id'],me['id'],me['id'],uid)).fetchall()
    return self.send(200,{'user':public_user(u),'stats':stats,'following':follow,'posts':rows})
   if path=='/api/profile-connections':
    username=q.get('username',[''])[0].strip().lower();kind=q.get('kind',['followers'])[0]
    if kind not in ('followers','following'):return self.fail('Invalid list')
    u=c.execute('SELECT id FROM users WHERE username=%s',(username,)).fetchone()
    if not u:return self.fail('User not found',404)
    if kind=='followers':
     rows=c.execute('SELECT users.* FROM follows JOIN users ON users.id=follows.follower WHERE follows.followed=%s ORDER BY users.id DESC LIMIT 100',(u['id'],)).fetchall()
    else:
     rows=c.execute('SELECT users.* FROM follows JOIN users ON users.id=follows.followed WHERE follows.follower=%s ORDER BY users.id DESC LIMIT 100',(u['id'],)).fetchall()
    return self.send(200,{'users':[public_user(r) for r in rows]})
   if path=='/api/notifications':
    rows=c.execute('SELECT n.*,u.username,u.name FROM notifications n JOIN users u ON u.id=n.actor WHERE n.recipient=%s ORDER BY n.id DESC LIMIT 100',(me['id'],)).fetchall()
    return self.send(200,{'notifications':[dict(r) for r in rows]})
   if path=='/api/stories':
    rows=c.execute('SELECT s.*,u.username,u.name FROM stories s JOIN users u ON u.id=s.user_id WHERE s.created>%s ORDER BY s.id DESC LIMIT 100',(now()-86400,)).fetchall()
    return self.send(200,{'stories':[dict(r) for r in rows]})
   if path=='/api/communities':
    rows=c.execute('SELECT c.*,u.username,(SELECT COUNT(*) FROM community_members WHERE community_id=c.id) members,EXISTS(SELECT 1 FROM community_members WHERE community_id=c.id AND user_id=%s) joined FROM communities c JOIN users u ON u.id=c.owner ORDER BY c.id DESC LIMIT 100',(me['id'],)).fetchall()
    return self.send(200,{'communities':[dict(r) for r in rows]})
   if path=='/api/community-posts':
    cid=q.get('id',['0'])[0];rows=c.execute('SELECT p.*,u.username,u.name FROM community_posts p JOIN users u ON u.id=p.user_id WHERE p.community_id=%s ORDER BY p.id DESC LIMIT 100',(cid,)).fetchall()
    return self.send(200,{'posts':[dict(r) for r in rows]})
   if path=='/api/users':
    search=q.get('q',[''])[0][:50];rows=c.execute('SELECT * FROM users WHERE username LIKE %s OR name LIKE %s ORDER BY id DESC LIMIT 40',('%'+search+'%','%'+search+'%')).fetchall()
    return self.send(200,{'users':[dict(**public_user(r),following=bool(c.execute('SELECT 1 FROM follows WHERE follower=%s AND followed=%s',(me['id'],r['id'])).fetchone())) for r in rows]})
   if path=='/api/posts':
    mode=q.get('mode',['all'])[0];search=q.get('q',[''])[0][:80];params=[me['id'],me['id'],me['id']];filter=''
    if mode=='following':filter=' AND (p.user_id=%s OR p.user_id IN (SELECT followed FROM follows WHERE follower=%s))';params += [me['id'],me['id']]
    if mode=='clips':filter+=" AND p.kind='clip'"
    if search:filter+=' AND (p.body LIKE %s OR u.username LIKE %s)';params += ['%'+search+'%','%'+search+'%']
    rows=c.execute('''SELECT p.*,u.username,u.name,u.bio,u.verified,u.avatar,p.pinned,(SELECT COUNT(*) FROM likes WHERE post_id=p.id) likes,(SELECT COUNT(*) FROM comments WHERE post_id=p.id) comments,EXISTS(SELECT 1 FROM likes WHERE post_id=p.id AND user_id=%s) liked,EXISTS(SELECT 1 FROM follows WHERE follower=%s AND followed=p.user_id) following,EXISTS(SELECT 1 FROM bookmarks WHERE user_id=%s AND post_id=p.id) bookmarked FROM posts p JOIN users u ON u.id=p.user_id WHERE 1=1'''+filter+' ORDER BY p.pinned DESC,p.created DESC,p.id DESC LIMIT 100',params).fetchall()
    return self.send(200,{'posts':[dict(r) for r in rows]})
   if path=='/api/comments':
    pid=q.get('post',['0'])[0];rows=c.execute('SELECT comments.*,users.username,users.name FROM comments JOIN users ON users.id=comments.user_id WHERE post_id=%s ORDER BY comments.id ASC LIMIT 150',(pid,)).fetchall();return self.send(200,{'comments':[dict(r) for r in rows]})
   if path=='/api/bookmarks':
    rows=c.execute('SELECT p.*,u.username,u.name,u.bio,u.verified,u.avatar,p.pinned,(SELECT COUNT(*) FROM likes WHERE post_id=p.id) likes,(SELECT COUNT(*) FROM comments WHERE post_id=p.id) comments,EXISTS(SELECT 1 FROM likes WHERE post_id=p.id AND user_id=%s) liked,EXISTS(SELECT 1 FROM follows WHERE follower=%s AND followed=p.user_id) following,1 bookmarked FROM bookmarks b JOIN posts p ON p.id=b.post_id JOIN users u ON u.id=p.user_id WHERE b.user_id=%s ORDER BY b.post_id DESC LIMIT 100',(me['id'],me['id'],me['id'])).fetchall()
    return self.send(200,{'posts':[dict(r) for r in rows]})
   if path=='/api/messages':
    other=q.get('user',['0'])[0];rows=c.execute('SELECT * FROM messages WHERE (sender=%s AND recipient=%s) OR (sender=%s AND recipient=%s) ORDER BY id DESC LIMIT 100',(me['id'],other,other,me['id'])).fetchall();return self.send(200,{'messages':[dict(r) for r in reversed(rows)]})
   return self.fail('Not found',404)
 def do_POST(self):
  path=urlparse(self.path).path;raw=self.body()
  if raw is None:return self.fail('Upload too large',413)
  with db() as c:
   me=self.identity(c)
   if path=='/api/upload':
    if not me:return self.fail('Log in first',401)
    if me['banned']:return self.fail('Account suspended',403)
    mime=self.headers.get('Content-Type','').split(';')[0];ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif','video/mp4':'.mp4','video/webm':'.webm'}.get(mime)
    if not ext or not raw:return self.fail('Use PNG, JPEG, WebP, GIF, MP4, or WebM')
    sig={'.png':raw.startswith(b'\x89PNG\r\n\x1a\n'),'.jpg':raw.startswith(b'\xff\xd8\xff'),'.webp':raw.startswith(b'RIFF') and raw[8:12]==b'WEBP','.gif':raw.startswith((b'GIF87a',b'GIF89a')),'.mp4':raw[4:8]==b'ftyp','.webm':raw.startswith(b'\x1a\x45\xdf\xa3')}
    if not sig[ext]:return self.fail('File does not match selected type')
    name=secrets.token_hex(18)+ext;c.execute('INSERT INTO media_files(name,owner_id,mime,content,created) VALUES(%s,%s,%s,%s,%s)',(name,me['id'],mime,raw,now()));return self.send(200,{'url':'/uploads/'+name,'kind':'clip' if ext in ('.mp4','.webm') else 'post'})
   try:data=json.loads(raw or b'{}')
   except Exception:return self.fail('Invalid JSON')
   if not isinstance(data,dict):return self.fail('Invalid request')
   if path=='/api/register':
    username=str(data.get('username','')).strip().lower();name=str(data.get('name','')).strip()[:40];password=str(data.get('password',''))
    if not (3<=len(username)<=20 and username.replace('_','').isalnum() and username.isascii()):return self.fail('Username must be 3–20 letters, digits, or underscores')
    if not name or len(password)<8:return self.fail('Enter a name and password of at least 8 characters')
    if c.execute('SELECT 1 FROM users WHERE username=%s',(username,)).fetchone():return self.fail('Username taken',409)
    salt=os.urandom(16);pw=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,260000)
    try:c.execute('INSERT INTO users(username,name,password,salt,created) VALUES(%s,%s,%s,%s,%s)',(username,name,pw,salt,now()))
    except UniqueViolation:return self.fail('Username taken',409)
    me=c.execute('SELECT * FROM users WHERE username=%s',(username,)).fetchone();return self.login(c,me)
   if path=='/api/login':
    username=str(data.get('username','')).lower();password=str(data.get('password',''));u=c.execute('SELECT * FROM users WHERE username=%s',(username,)).fetchone()
    if not u or u['banned'] or not hmac.compare_digest(bytes(u['password']),hashlib.pbkdf2_hmac('sha256',password.encode(),bytes(u['salt']),260000)):return self.fail('Incorrect username or password',401)
    return self.login(c,u)
   if path=='/api/logout':
    cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''));token=cookie.get('pulse_session')
    if token:c.execute('DELETE FROM sessions WHERE token=%s',(token.value,))
    return self.send(200,{'ok':True},{'Set-Cookie':'pulse_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'})
   if not me:return self.fail('Log in first',401)
   if path=='/api/group-create':
    name=str(data.get('name','')).strip()[:60]
    if len(name)<2:return self.fail('Group name must be at least 2 characters')
    gid=c.execute('INSERT INTO chat_groups(name,owner,created) VALUES(%s,%s,%s) RETURNING id',(name,me['id'],now())).fetchone()['id']
    c.execute('INSERT INTO chat_members(group_id,user_id) VALUES(%s,%s)',(gid,me['id']))
    return self.send(200,{'id':gid})
   if path in ('/api/group-invite','/api/group-message','/api/group-leave','/api/voice-presence','/api/voice-signal'):
    try:gid=int(data.get('group',0))
    except (ValueError,TypeError):return self.fail('Invalid group')
    if not c.execute('SELECT 1 FROM chat_members WHERE group_id=%s AND user_id=%s',(gid,me['id'])).fetchone():return self.fail('Not a member',403)
    if path=='/api/group-invite':
     username=str(data.get('username','')).strip().lower()
     person=c.execute('SELECT id FROM users WHERE username=%s',(username,)).fetchone()
     if not person:return self.fail('User not found',404)
     if c.execute('SELECT 1 FROM chat_members WHERE group_id=%s AND user_id=%s',(gid,person['id'])).fetchone():return self.fail('Already in group')
     c.execute('INSERT INTO chat_members(group_id,user_id) VALUES(%s,%s)',(gid,person['id']))
     notify(c,person['id'],me['id'],'group','Added you to a group chat')
     return self.send(200,{'ok':True})
    if path=='/api/group-message':
     msg=str(data.get('body','')).strip()
     if not msg or len(msg)>2000:return self.fail('Message must be 1–2000 characters')
     c.execute('INSERT INTO chat_messages(group_id,user_id,body,created) VALUES(%s,%s,%s,%s)',(gid,me['id'],msg,now()))
     return self.send(200,{'ok':True})
    if path=='/api/group-leave':
     c.execute('DELETE FROM voice_presence WHERE group_id=%s AND user_id=%s',(gid,me['id']))
     c.execute('DELETE FROM chat_members WHERE group_id=%s AND user_id=%s',(gid,me['id']))
     return self.send(200,{'ok':True})
    if path=='/api/voice-presence':
     if data.get('active'):
      c.execute('INSERT INTO voice_presence(group_id,user_id,updated) VALUES(%s,%s,%s) ON CONFLICT(group_id,user_id) DO UPDATE SET updated=EXCLUDED.updated',(gid,me['id'],now()))
     else:c.execute('DELETE FROM voice_presence WHERE group_id=%s AND user_id=%s',(gid,me['id']))
     return self.send(200,{'ok':True})
    if path=='/api/voice-signal':
     try:recipient=int(data.get('recipient',0))
     except (ValueError,TypeError):return self.fail('Invalid recipient')
     if recipient==me['id'] or not c.execute('SELECT 1 FROM chat_members WHERE group_id=%s AND user_id=%s',(gid,recipient)).fetchone():return self.fail('Invalid recipient',403)
     payload=json.dumps(data.get('payload'))
     if len(payload)>16000:return self.fail('Signal too large')
     c.execute('INSERT INTO voice_signals(group_id,sender,recipient,payload,created) VALUES(%s,%s,%s,%s,%s)',(gid,me['id'],recipient,payload,now()))
     return self.send(200,{'ok':True})
   if path=='/api/admin/site-settings':
    if not is_admin(me):return self.fail('Admin only',403)
    key=str(data.get('key',''))
    if key not in ('announcement','theme'):return self.fail('Invalid setting')
    value=str(data.get('value','')).strip()
    if key=='announcement' and len(value)>240:return self.fail('Announcement too long')
    if key=='theme' and value not in ('purple','blue','green','pink','orange','red'):return self.fail('Invalid theme')
    c.execute('INSERT INTO site_settings(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',(key,value))
    return self.send(200,{'ok':True})
   if path=='/api/report':
    pid=data.get('post');reason=str(data.get('reason','')).strip()[:500]
    if not reason:return self.fail('Report reason required')
    if not c.execute('SELECT 1 FROM posts WHERE id=%s',(pid,)).fetchone():return self.fail('Post not found',404)
    c.execute('INSERT INTO reports(reporter_id,post_id,reason,created) VALUES(%s,%s,%s,%s)',(me['id'],pid,reason,now()))
    return self.send(200,{'ok':True})
   if path=='/api/admin/action':
    if not is_admin(me):return self.fail('Admin only',403)
    operation=str(data.get('action',''));target=data.get('id')
    try:target=int(target)
    except (TypeError,ValueError):return self.fail('Invalid ID')
    if operation=='resolve_report':c.execute('UPDATE reports SET resolved=1 WHERE id=%s',(target,))
    elif operation=='delete_post':c.execute('DELETE FROM posts WHERE id=%s',(target,))
    elif operation=='verify':c.execute('UPDATE users SET verified=1-verified WHERE id=%s',(target,))
    elif operation=='ban':
     if target==me['id']:return self.fail('Cannot suspend yourself')
     c.execute('UPDATE users SET banned=1-banned WHERE id=%s',(target,))
     c.execute('DELETE FROM sessions WHERE user_id=%s AND (SELECT banned FROM users WHERE id=%s)=1',(target,target))
    elif operation=='pin':c.execute('UPDATE posts SET pinned=1-pinned WHERE id=%s',(target,))
    else:return self.fail('Unknown admin action')
    return self.send(200,{'ok':True})
   if me['banned']:return self.fail('Account suspended',403)
   uid=me['id'];body=str(data.get('body','')).strip()
   if path=='/api/bookmark':
    pid=data.get('post')
    if not c.execute('SELECT 1 FROM posts WHERE id=%s',(pid,)).fetchone():return self.fail('Post not found',404)
    if c.execute('SELECT 1 FROM bookmarks WHERE user_id=%s AND post_id=%s',(uid,pid)).fetchone():c.execute('DELETE FROM bookmarks WHERE user_id=%s AND post_id=%s',(uid,pid))
    else:c.execute('INSERT INTO bookmarks VALUES(%s,%s)',(uid,pid))
    return self.send(200,{'ok':True})
   if path=='/api/story':
    media=str(data.get('media',''))
    if media and (not media.startswith('/uploads/') or not (c.execute('SELECT 1 FROM media_files WHERE name=%s',(Path(media).name,)).fetchone() or (UP/Path(media).name).is_file())):return self.fail('Invalid media')
    if len(body)>250 or not (body or media):return self.fail('Story needs text or media (max 250 characters)')
    c.execute('INSERT INTO stories(user_id,body,media,created) VALUES(%s,%s,%s,%s)',(uid,body,media,now()));return self.send(200,{'ok':True})
   if path=='/api/community-create':
    name=str(data.get('name','')).strip()[:45];description=str(data.get('description','')).strip()[:250]
    if len(name)<3:return self.fail('Community name must be at least 3 characters')
    try:cur=c.execute('INSERT INTO communities(name,description,owner,created) VALUES(%s,%s,%s,%s) RETURNING id',(name,description,uid,now()))
    except UniqueViolation:return self.fail('Community name already exists',409)
    c.execute('INSERT INTO community_members VALUES(%s,%s)',(cur.fetchone()['id'],uid));return self.send(200,{'ok':True})
   if path=='/api/community-join':
    cid=data.get('id')
    if not c.execute('SELECT 1 FROM communities WHERE id=%s',(cid,)).fetchone():return self.fail('Community not found',404)
    if c.execute('SELECT 1 FROM community_members WHERE community_id=%s AND user_id=%s',(cid,uid)).fetchone():c.execute('DELETE FROM community_members WHERE community_id=%s AND user_id=%s',(cid,uid))
    else:c.execute('INSERT INTO community_members VALUES(%s,%s)',(cid,uid))
    return self.send(200,{'ok':True})
   if path=='/api/community-post':
    cid=data.get('id')
    if not c.execute('SELECT 1 FROM community_members WHERE community_id=%s AND user_id=%s',(cid,uid)).fetchone():return self.fail('Join the community first',403)
    if not body or len(body)>1000:return self.fail('Post must be 1–1000 characters')
    c.execute('INSERT INTO community_posts(community_id,user_id,body,created) VALUES(%s,%s,%s,%s)',(cid,uid,body,now()));return self.send(200,{'ok':True})
   if path=='/api/notifications-read':
    c.execute('UPDATE notifications SET seen=1 WHERE recipient=%s',(uid,));return self.send(200,{'ok':True})
   if path=='/api/posts':
    media=str(data.get('media',''));kind='clip' if media.endswith(('.mp4','.webm')) else 'post'
    if media and (not media.startswith('/uploads/') or not (c.execute('SELECT 1 FROM media_files WHERE name=%s',(Path(media).name,)).fetchone() or (UP/Path(media).name).is_file())):return self.fail('Invalid media')
    if len(body)>1000 or (not body and not media):return self.fail('Write a post (max 1000 characters) or upload media')
    request_id=str(data.get('request_id',''))
    if request_id and not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}',request_id):return self.fail('Invalid request ID')
    if request_id:
     c.execute('INSERT INTO posts(user_id,body,media,kind,created,request_id) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id,request_id) WHERE request_id IS NOT NULL DO NOTHING',(uid,body,media,kind,now(),request_id))
    else:c.execute('INSERT INTO posts(user_id,body,media,kind,created) VALUES(%s,%s,%s,%s,%s)',(uid,body,media,kind,now()))
    return self.send(200,{'ok':True})
   if path=='/api/delete':
    pid=data.get('post');c.execute('DELETE FROM posts WHERE id=%s AND user_id=%s',(pid,uid));return self.send(200,{'ok':True})
   if path=='/api/like':
    pid=data.get('post');exists=c.execute('SELECT 1 FROM posts WHERE id=%s',(pid,)).fetchone()
    if not exists:return self.fail('Post not found',404)
    if c.execute('SELECT 1 FROM likes WHERE user_id=%s AND post_id=%s',(uid,pid)).fetchone():c.execute('DELETE FROM likes WHERE user_id=%s AND post_id=%s',(uid,pid))
    else:c.execute('INSERT INTO likes VALUES(%s,%s)',(uid,pid))
    owner=c.execute('SELECT user_id FROM posts WHERE id=%s',(pid,)).fetchone();notify(c,owner['user_id'],uid,'like','post '+str(pid)) if owner else None
    return self.send(200,{'ok':True})
   if path=='/api/comment':
    if not body or len(body)>500:return self.fail('Comment must be 1–500 characters')
    if not c.execute('SELECT 1 FROM posts WHERE id=%s',(data.get('post'),)).fetchone():return self.fail('Post not found',404)
    c.execute('INSERT INTO comments(post_id,user_id,body,created) VALUES(%s,%s,%s,%s)',(data.get('post'),uid,body,now()));owner=c.execute('SELECT user_id FROM posts WHERE id=%s',(data.get('post'),)).fetchone();notify(c,owner['user_id'],uid,'comment','post '+str(data.get('post')));return self.send(200,{'ok':True})
   if path=='/api/follow':
    target=data.get('user');
    if target==uid or not c.execute('SELECT 1 FROM users WHERE id=%s',(target,)).fetchone():return self.fail('Invalid user')
    if c.execute('SELECT 1 FROM follows WHERE follower=%s AND followed=%s',(uid,target)).fetchone():c.execute('DELETE FROM follows WHERE follower=%s AND followed=%s',(uid,target))
    else:c.execute('INSERT INTO follows VALUES(%s,%s)',(uid,target))
    notify(c,target,uid,'follow')
    return self.send(200,{'ok':True})
   if path=='/api/message':
    target=data.get('user');
    if not body or len(body)>1000:return self.fail('Message must be 1–1000 characters')
    if target==uid or not c.execute('SELECT 1 FROM users WHERE id=%s',(target,)).fetchone():return self.fail('Invalid recipient')
    c.execute('INSERT INTO messages(sender,recipient,body,created) VALUES(%s,%s,%s,%s)',(uid,target,body,now()));notify(c,target,uid,'message');return self.send(200,{'ok':True})
   if path=='/api/profile':
    name=str(data.get('name','')).strip()[:40];bio=str(data.get('bio','')).strip()[:200]
    if not name:return self.fail('Name required')
    avatar=str(data.get('avatar','')).strip();banner=str(data.get('banner','')).strip();website=str(data.get('website','')).strip()[:200]
    for media in (avatar,banner):
     if media and (not re.fullmatch(r'/uploads/[a-f0-9]{36}\.(?:png|jpg|webp|gif)',media) or not (c.execute('SELECT 1 FROM media_files WHERE name=%s',(Path(media).name,)).fetchone() or (UP/Path(media).name).is_file())):return self.fail('Invalid profile image')
    if website and not (website.startswith('https://') or website.startswith('http://')):return self.fail('Website must start with https:// or http://')
    c.execute('UPDATE users SET name=%s,bio=%s,avatar=%s,banner=%s,website=%s WHERE id=%s',(name,bio,avatar,banner,website,uid));return self.send(200,{'ok':True})
   return self.fail('Not found',404)
 def login(self,c,u):
  token=secrets.token_urlsafe(32);c.execute('INSERT INTO sessions VALUES(%s,%s,%s)',(token,u['id'],now()+60*60*24*14));secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else '';return self.send(200,{'user':public_user(u)},{'Set-Cookie':f'pulse_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=1209600{secure}'})
init_db()
migrate_legacy_media()
if __name__=='__main__':
 port=int(os.environ.get('PORT','8000'));print(f'Pulse v0.5.1 running at http://localhost:{port}');ThreadingHTTPServer(('0.0.0.0',port),App).serve_forever()
