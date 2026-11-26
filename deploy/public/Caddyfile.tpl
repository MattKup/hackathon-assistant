# Шаблон: __HOST__ — публичный адрес (PUBLIC_HOST), __HASH__ — bcrypt-хэш пароля (генерируется на стенде, не в git).
# Роутер: проброс 8090/80 и 44435/443 — Caddy слушает оба варианта портов на стенде (схема проброса не важна).
{
	admin off
	auto_https disable_redirects
	skip_install_trust
	default_sni __HOST__
}

http://:80, http://:8090 {
	basic_auth {
		jury __HASH__
	}
	reverse_proxy 127.0.0.1:19650
}

https://__HOST__:443, https://__HOST__:44435 {
	tls internal
	basic_auth {
		jury __HASH__
	}
	reverse_proxy 127.0.0.1:19650
}
