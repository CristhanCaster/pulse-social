import os, sqlite3, json, secrets, hashlib, hmac, mimetypes, time, threading
from collections import defaultdict, deque
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from http.cookies import SimpleCookie
ROOT=Path(__file__).resolve().parent
DATA=Path(os.environ.get('PULSE_DATA_DIR',str(ROOT))).resolve(); DATA.mkdir(parents=True,exist_ok=True)
DB=DATA/'pulse.db'; UP=DATA/'uploads'; UP.mkdir(exist_ok=True)
ADMIN_ID=os.environ.get('PULSE_ADMIN_USER_ID','').strip()
RATE=defaultdict(deque); RATE_LOCK=threading.Lock()
MAX=12*1024*1024

def db():
 c=sqlite3.connect(DB,timeout=20);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA journal_mode=WAL');return c
with db() as c:
 c.executescript('''CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,bio TEXT DEFAULT '',password BLOB NOT NULL,salt BLOB NOT NULL,created INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),expires INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS posts(id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),body TEXT NOT NULL,media TEXT DEFAULT '',kind TEXT DEFAULT 'post',created INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS likes(user_id INTEGER REFERENCES users(id),post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,PRIMARY KEY(user_id,post_id));
 CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY,post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id),body TEXT NOT NULL,created INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS follows(follower INTEGER REFERENCES users(id),followed INTEGER REFERENCES users(id),PRIMARY KEY(follower,followed));
 CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY,sender INTEGER REFERENCES users(id),recipient INTEGER REFERENCES users(id),body TEXT NOT NULL,created INTEGER NOT NULL);
 CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created DESC); CREATE INDEX IF NOT EXISTS idx_msg ON messages(sender,recipient,created);''')
with db() as c:
 c.executescript("""CREATE TABLE IF NOT EXISTS bookmarks(user_id INTEGER REFERENCES users(id),post_id INTEGER REFERENCES posts(id) ON DELETE CASCADE,PRIMARY KEY(user_id,post_id));
 CREATE TABLE IF NOT EXISTS stories(id INTEGER PRIMARY KEY,user_id INTEGER REFERENCES users(id),body TEXT NOT NULL DEFAULT '',media TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS communities(id INTEGER PRIMARY KEY,name TEXT UNIQUE NOT NULL,description TEXT NOT NULL,owner INTEGER REFERENCES users(id),created INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS community_members(community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id),PRIMARY KEY(community_id,user_id));
 CREATE TABLE IF NOT EXISTS community_posts(id INTEGER PRIMARY KEY,community_id INTEGER REFERENCES communities(id) ON DELETE CASCADE,user_id INTEGER REFERENCES users(id),body TEXT NOT NULL,created INTEGER NOT NULL);
 CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY,recipient INTEGER REFERENCES users(id),actor INTEGER REFERENCES users(id),kind TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',created INTEGER NOT NULL,seen INTEGER NOT NULL DEFAULT 0);
 CREATE INDEX IF NOT EXISTS idx_notifications ON notifications(recipient,id DESC);
 CREATE INDEX IF NOT EXISTS idx_stories ON stories(created DESC);""")
def notify(c,recipient,actor,kind,detail=''):
 if recipient!=actor:c.execute('INSERT INTO notifications(recipient,actor,kind,detail,created) VALUES(?,?,?,?,?)',(recipient,actor,kind,str(detail)[:100],now()))
def now():return int(time.time())
def public_user(row):
    keys = row.keys()
    return dict(
        id=row['id'],
        username=row['username'],
        name=row['name'],
        bio=row['bio'],
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
  return c.execute('SELECT users.* FROM users JOIN sessions ON sessions.user_id=users.id WHERE sessions.token=? AND sessions.expires>?',(token,now())).fetchone()
 def body(self):
  size=int(self.headers.get('Content-Length','0'))
  if size>MAX+1024:return None
  return self.rfile.read(size)
 def do_GET(self):
  path=urlparse(self.path).path
  if path=='/' or path.startswith('/static/') or path.startswith('/uploads/'):
   target=ROOT/'static'/'index.html' if path=='/' else ROOT/path.lstrip('/')
   target=target.resolve()
   if not (target.is_relative_to((ROOT/'static').resolve()) or target.is_relative_to(UP.resolve())) or not target.is_file():return self.send_error(404)
   mime=mimetypes.guess_type(str(target))[0] or 'application/octet-stream';data=target.read_bytes();self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(data)));self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(data);return
  with db() as c:
   me=self.identity(c)
   if path=='/api/me':return self.send(200,{'user':public_user(me) if me else None})
   if not me:return self.fail('Log in first',401)
   q=parse_qs(urlparse(self.path).query)
   if path=='/api/notifications':
    rows=c.execute('SELECT n.*,u.username,u.name FROM notifications n JOIN users u ON u.id=n.actor WHERE n.recipient=? ORDER BY n.id DESC LIMIT 100',(me['id'],)).fetchall()
    return self.send(200,{'notifications':[dict(r) for r in rows]})
   if path=='/api/stories':
    rows=c.execute('SELECT s.*,u.username,u.name FROM stories s JOIN users u ON u.id=s.user_id WHERE s.created>? ORDER BY s.id DESC LIMIT 100',(now()-86400,)).fetchall()
    return self.send(200,{'stories':[dict(r) for r in rows]})
   if path=='/api/communities':
    rows=c.execute('SELECT c.*,u.username,(SELECT COUNT(*) FROM community_members WHERE community_id=c.id) members,EXISTS(SELECT 1 FROM community_members WHERE community_id=c.id AND user_id=?) joined FROM communities c JOIN users u ON u.id=c.owner ORDER BY c.id DESC LIMIT 100',(me['id'],)).fetchall()
    return self.send(200,{'communities':[dict(r) for r in rows]})
   if path=='/api/community-posts':
    cid=q.get('id',['0'])[0];rows=c.execute('SELECT p.*,u.username,u.name FROM community_posts p JOIN users u ON u.id=p.user_id WHERE p.community_id=? ORDER BY p.id DESC LIMIT 100',(cid,)).fetchall()
    return self.send(200,{'posts':[dict(r) for r in rows]})
   if path=='/api/users':
    search=q.get('q',[''])[0][:50];rows=c.execute('SELECT * FROM users WHERE username LIKE ? OR name LIKE ? ORDER BY id DESC LIMIT 40',('%'+search+'%','%'+search+'%')).fetchall()
    return self.send(200,{'users':[dict(**public_user(r),following=bool(c.execute('SELECT 1 FROM follows WHERE follower=? AND followed=?',(me['id'],r['id'])).fetchone())) for r in rows]})
   if path=='/api/posts':
    mode=q.get('mode',['all'])[0];search=q.get('q',[''])[0][:80];params=[me['id'],me['id'],me['id']];filter=''
    if mode=='following':filter=' AND (p.user_id=? OR p.user_id IN (SELECT followed FROM follows WHERE follower=?))';params += [me['id'],me['id']]
    if mode=='clips':filter+=" AND p.kind='clip'"
    if search:filter+=' AND (p.body LIKE ? OR u.username LIKE ?)';params += ['%'+search+'%','%'+search+'%']
    rows=c.execute('''SELECT p.*,u.username,u.name,u.bio,u.verified,p.pinned,(SELECT COUNT(*) FROM likes WHERE post_id=p.id) likes,(SELECT COUNT(*) FROM comments WHERE post_id=p.id) comments,EXISTS(SELECT 1 FROM likes WHERE post_id=p.id AND user_id=?) liked,EXISTS(SELECT 1 FROM follows WHERE follower=? AND followed=p.user_id) following,EXISTS(SELECT 1 FROM bookmarks WHERE user_id=? AND post_id=p.id) bookmarked FROM posts p JOIN users u ON u.id=p.user_id WHERE 1=1'''+filter+' ORDER BY p.pinned DESC,p.created DESC,p.id DESC LIMIT 100',params).fetchall()
    return self.send(200,{'posts':[dict(r) for r in rows]})
   if path=='/api/comments':
    pid=q.get('post',['0'])[0];rows=c.execute('SELECT comments.*,users.username,users.name FROM comments JOIN users ON users.id=comments.user_id WHERE post_id=? ORDER BY comments.id ASC LIMIT 150',(pid,)).fetchall();return self.send(200,{'comments':[dict(r) for r in rows]})
   if path=='/api/bookmarks':
    rows=c.execute('SELECT p.*,u.username,u.name,u.bio,u.verified,p.pinned,(SELECT COUNT(*) FROM likes WHERE post_id=p.id) likes,(SELECT COUNT(*) FROM comments WHERE post_id=p.id) comments,EXISTS(SELECT 1 FROM likes WHERE post_id=p.id AND user_id=?) liked,EXISTS(SELECT 1 FROM follows WHERE follower=? AND followed=p.user_id) following,1 bookmarked FROM bookmarks b JOIN posts p ON p.id=b.post_id JOIN users u ON u.id=p.user_id WHERE b.user_id=? ORDER BY b.rowid DESC LIMIT 100',(me['id'],me['id'],me['id'])).fetchall()
    return self.send(200,{'posts':[dict(r) for r in rows]})
   if path=='/api/messages':
    other=q.get('user',['0'])[0];rows=c.execute('SELECT * FROM messages WHERE (sender=? AND recipient=?) OR (sender=? AND recipient=?) ORDER BY id DESC LIMIT 100',(me['id'],other,other,me['id'])).fetchall();return self.send(200,{'messages':[dict(r) for r in reversed(rows)]})
   return self.fail('Not found',404)
 def do_POST(self):
  path=urlparse(self.path).path;raw=self.body()
  if raw is None:return self.fail('Upload too large',413)
  with db() as c:
   me=self.identity(c)
   if path=='/api/upload':
    if not me:return self.fail('Log in first',401)
    if me['banned']:return self.fail('Account suspended',403)
    if me['banned']:return self.fail('Account suspended',403)
    mime=self.headers.get('Content-Type','').split(';')[0];ext={'image/png':'.png','image/jpeg':'.jpg','image/webp':'.webp','image/gif':'.gif','video/mp4':'.mp4','video/webm':'.webm'}.get(mime)
    if not ext or not raw:return self.fail('Use PNG, JPEG, WebP, GIF, MP4, or WebM')
    sig={'.png':raw.startswith(b'\x89PNG\r\n\x1a\n'),'.jpg':raw.startswith(b'\xff\xd8\xff'),'.webp':raw.startswith(b'RIFF') and raw[8:12]==b'WEBP','.gif':raw.startswith((b'GIF87a',b'GIF89a')),'.mp4':raw[4:8]==b'ftyp','.webm':raw.startswith(b'\x1a\x45\xdf\xa3')}
    if not sig[ext]:return self.fail('File does not match selected type')
    name=secrets.token_hex(18)+ext;(UP/name).write_bytes(raw);return self.send(200,{'url':'/uploads/'+name,'kind':'clip' if ext in ('.mp4','.webm') else 'post'})
   try:data=json.loads(raw or b'{}')
   except Exception:return self.fail('Invalid JSON')
   if not isinstance(data,dict):return self.fail('Invalid request')
   if path=='/api/register':
    username=str(data.get('username','')).strip().lower();name=str(data.get('name','')).strip()[:40];password=str(data.get('password',''))
    if not (3<=len(username)<=20 and username.replace('_','').isalnum() and username.isascii()):return self.fail('Username must be 3–20 letters, digits, or underscores')
    if not name or len(password)<8:return self.fail('Enter a name and password of at least 8 characters')
    salt=os.urandom(16);pw=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,260000)
    try:c.execute('INSERT INTO users(username,name,password,salt,created) VALUES(?,?,?,?,?)',(username,name,pw,salt,now()))
    except sqlite3.IntegrityError:return self.fail('Username taken',409)
    me=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone();return self.login(c,me)
   if path=='/api/login':
    username=str(data.get('username','')).lower();password=str(data.get('password',''));u=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
    if not u or u['banned'] or not hmac.compare_digest(u['password'],hashlib.pbkdf2_hmac('sha256',password.encode(),u['salt'],260000)):return self.fail('Incorrect username or password',401)
    return self.login(c,u)
   if path=='/api/logout':
    cookie=SimpleCookie();cookie.load(self.headers.get('Cookie',''));token=cookie.get('pulse_session')
    if token:c.execute('DELETE FROM sessions WHERE token=?',(token.value,))
    return self.send(200,{'ok':True},{'Set-Cookie':'pulse_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'})
   if not me:return self.fail('Log in first',401)
   uid=me['id'];body=str(data.get('body','')).strip()
   if path=='/api/bookmark':
    pid=data.get('post')
    if not c.execute('SELECT 1 FROM posts WHERE id=?',(pid,)).fetchone():return self.fail('Post not found',404)
    if c.execute('SELECT 1 FROM bookmarks WHERE user_id=? AND post_id=?',(uid,pid)).fetchone():c.execute('DELETE FROM bookmarks WHERE user_id=? AND post_id=?',(uid,pid))
    else:c.execute('INSERT INTO bookmarks VALUES(?,?)',(uid,pid))
    return self.send(200,{'ok':True})
   if path=='/api/story':
    media=str(data.get('media',''))
    if media and (not media.startswith('/uploads/') or not (UP/Path(media).name).is_file()):return self.fail('Invalid media')
    if len(body)>250 or not (body or media):return self.fail('Story needs text or media (max 250 characters)')
    c.execute('INSERT INTO stories(user_id,body,media,created) VALUES(?,?,?,?)',(uid,body,media,now()));return self.send(200,{'ok':True})
   if path=='/api/community-create':
    name=str(data.get('name','')).strip()[:45];description=str(data.get('description','')).strip()[:250]
    if len(name)<3:return self.fail('Community name must be at least 3 characters')
    try:cur=c.execute('INSERT INTO communities(name,description,owner,created) VALUES(?,?,?,?)',(name,description,uid,now()))
    except sqlite3.IntegrityError:return self.fail('Community name already exists',409)
    c.execute('INSERT INTO community_members VALUES(?,?)',(cur.lastrowid,uid));return self.send(200,{'ok':True})
   if path=='/api/community-join':
    cid=data.get('id')
    if not c.execute('SELECT 1 FROM communities WHERE id=?',(cid,)).fetchone():return self.fail('Community not found',404)
    if c.execute('SELECT 1 FROM community_members WHERE community_id=? AND user_id=?',(cid,uid)).fetchone():c.execute('DELETE FROM community_members WHERE community_id=? AND user_id=?',(cid,uid))
    else:c.execute('INSERT INTO community_members VALUES(?,?)',(cid,uid))
    return self.send(200,{'ok':True})
   if path=='/api/community-post':
    cid=data.get('id')
    if not c.execute('SELECT 1 FROM community_members WHERE community_id=? AND user_id=?',(cid,uid)).fetchone():return self.fail('Join the community first',403)
    if not body or len(body)>1000:return self.fail('Post must be 1–1000 characters')
    c.execute('INSERT INTO community_posts(community_id,user_id,body,created) VALUES(?,?,?,?)',(cid,uid,body,now()));return self.send(200,{'ok':True})
   if path=='/api/notifications-read':
    c.execute('UPDATE notifications SET seen=1 WHERE recipient=?',(uid,));return self.send(200,{'ok':True})
   if path=='/api/posts':
    media=str(data.get('media',''));kind='clip' if media.endswith(('.mp4','.webm')) else 'post'
    if media and (not media.startswith('/uploads/') or not (UP/Path(media).name).is_file()):return self.fail('Invalid media')
    if len(body)>1000 or (not body and not media):return self.fail('Write a post (max 1000 characters) or upload media')
    c.execute('INSERT INTO posts(user_id,body,media,kind,created) VALUES(?,?,?,?,?)',(uid,body,media,kind,now()));return self.send(200,{'ok':True})
   if path=='/api/delete':
    pid=data.get('post');c.execute('DELETE FROM posts WHERE id=? AND user_id=?',(pid,uid));return self.send(200,{'ok':True})
   if path=='/api/like':
    pid=data.get('post');exists=c.execute('SELECT 1 FROM posts WHERE id=?',(pid,)).fetchone()
    if not exists:return self.fail('Post not found',404)
    if c.execute('SELECT 1 FROM likes WHERE user_id=? AND post_id=?',(uid,pid)).fetchone():c.execute('DELETE FROM likes WHERE user_id=? AND post_id=?',(uid,pid))
    else:c.execute('INSERT INTO likes VALUES(?,?)',(uid,pid))
    owner=c.execute('SELECT user_id FROM posts WHERE id=?',(pid,)).fetchone();notify(c,owner['user_id'],uid,'like','post '+str(pid)) if owner else None
    return self.send(200,{'ok':True})
   if path=='/api/comment':
    if not body or len(body)>500:return self.fail('Comment must be 1–500 characters')
    if not c.execute('SELECT 1 FROM posts WHERE id=?',(data.get('post'),)).fetchone():return self.fail('Post not found',404)
    c.execute('INSERT INTO comments(post_id,user_id,body,created) VALUES(?,?,?,?)',(data.get('post'),uid,body,now()));owner=c.execute('SELECT user_id FROM posts WHERE id=?',(data.get('post'),)).fetchone();notify(c,owner['user_id'],uid,'comment','post '+str(data.get('post')));return self.send(200,{'ok':True})
   if path=='/api/follow':
    target=data.get('user');
    if target==uid or not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():return self.fail('Invalid user')
    if c.execute('SELECT 1 FROM follows WHERE follower=? AND followed=?',(uid,target)).fetchone():c.execute('DELETE FROM follows WHERE follower=? AND followed=?',(uid,target))
    else:c.execute('INSERT INTO follows VALUES(?,?)',(uid,target))
    notify(c,target,uid,'follow')
    return self.send(200,{'ok':True})
   if path=='/api/message':
    target=data.get('user');
    if not body or len(body)>1000:return self.fail('Message must be 1–1000 characters')
    if target==uid or not c.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():return self.fail('Invalid recipient')
    c.execute('INSERT INTO messages(sender,recipient,body,created) VALUES(?,?,?,?)',(uid,target,body,now()));notify(c,target,uid,'message');return self.send(200,{'ok':True})
   if path=='/api/profile':
    name=str(data.get('name','')).strip()[:40];bio=str(data.get('bio','')).strip()[:200]
    if not name:return self.fail('Name required')
    c.execute('UPDATE users SET name=?,bio=? WHERE id=?',(name,bio,uid));return self.send(200,{'ok':True})
   return self.fail('Not found',404)
 def login(self,c,u):
  token=secrets.token_urlsafe(32);c.execute('INSERT INTO sessions VALUES(?,?,?)',(token,u['id'],now()+60*60*24*14));secure='; Secure' if self.headers.get('X-Forwarded-Proto','').lower()=='https' else '';return self.send(200,{'user':public_user(u)},{'Set-Cookie':f'pulse_session={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=1209600{secure}'})
if __name__=='__main__':
 port=int(os.environ.get('PORT','8000'));print(f'Pulse v0.5 running at http://localhost:{port}');ThreadingHTTPServer(('0.0.0.0',port),App).serve_forever()
