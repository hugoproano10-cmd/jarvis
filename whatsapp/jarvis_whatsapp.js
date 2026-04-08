const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const axios = require('axios');
const fs = require('fs');
const path = require('path');

// Aceptar cualquier formato del número de Hugo
const NUMERO_AUTORIZADO = '157316078956710@lid';
const JARVIS_BOT_URL = 'http://localhost:8000/mensaje';

// Buscar Chromium instalado en el sistema
const chromiumPath = require('child_process')
    .execSync('which chromium-browser || which chromium || which google-chrome')
    .toString().trim();

const client = new Client({
    authStrategy: new LocalAuth(),
    puppeteer: {
        executablePath: chromiumPath,
        args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
    }
});

client.on('qr', qr => {
    console.log('Escanea este QR con WhatsApp:');
    qrcode.generate(qr, { small: true });
});

client.on('ready', () => {
    console.log('JARVIS WhatsApp listo!');
});

client.on('message_create', msg => {
    console.log('MSG RECIBIDO:', msg.from, msg.to, msg.body);
});

// Mensajes de texto
client.on('message', async msg => {
    if (msg.from !== NUMERO_AUTORIZADO && !msg.from.includes("157316078956710")) return;

    // Ignorar mensajes de audio (se manejan aparte)
    if (msg.hasMedia && (msg.type === 'ptt' || msg.type === 'audio')) {
        return handleVoice(msg);
    }

    if (!msg.body || msg.body.trim() === '') return;

    try {
        await msg.react('🤔');
        const resp = await axios.post(JARVIS_BOT_URL, {
            mensaje: msg.body,
            tipo: 'texto'
        }, { timeout: 180000 });

        const data = resp.data;
        await msg.reply(data.respuesta);

        // Enviar audio si está disponible
        if (data.audio_base64) {
            try {
                const media = new MessageMedia('audio/mpeg', data.audio_base64, 'jarvis.mp3');
                await msg.reply(media, undefined, { sendAudioAsVoice: true });
            } catch (audioErr) {
                console.log('Error enviando audio:', audioErr.message);
            }
        }

        await msg.react('✅');
    } catch (e) {
        console.error('Error:', e.message);
        await msg.reply('Error: ' + e.message);
    }
});

// Mensajes de voz
async function handleVoice(msg) {
    try {
        await msg.react('🎤');

        // Descargar audio
        const media = await msg.downloadMedia();
        if (!media) {
            await msg.reply('No pude descargar el audio.');
            return;
        }

        // Enviar a JARVIS con audio base64
        const resp = await axios.post(JARVIS_BOT_URL, {
            mensaje: '',
            tipo: 'audio',
            audio_base64: media.data
        }, { timeout: 300000 });

        const data = resp.data;

        // Mostrar transcripción
        if (data.transcripcion) {
            await msg.reply(`🎤 _${data.transcripcion}_`);
        }

        // Enviar respuesta texto
        await msg.reply(data.respuesta);

        // Enviar respuesta audio
        if (data.audio_base64) {
            try {
                const audioMedia = new MessageMedia('audio/mpeg', data.audio_base64, 'jarvis.mp3');
                await msg.reply(audioMedia, undefined, { sendAudioAsVoice: true });
            } catch (audioErr) {
                console.log('Error enviando audio respuesta:', audioErr.message);
            }
        }

        await msg.react('✅');
    } catch (e) {
        console.error('Error voz:', e.message);
        await msg.reply('Error procesando audio: ' + e.message);
    }
}

client.initialize();

// ── Servidor HTTP para alertas salientes de JARVIS (puerto 8001) ──
const http = require('http');
const alertServer = http.createServer((req, res) => {
    if (req.method === 'POST' && req.url === '/alerta') {
        let body = '';
        let size = 0;
        const MAX_SIZE = 50 * 1024 * 1024; // 50MB
        req.on('data', chunk => {
            size += chunk.length;
            if (size > MAX_SIZE) {
                res.writeHead(413, {'Content-Type': 'application/json'});
                res.end(JSON.stringify({error: 'payload too large'}));
                req.destroy();
                return;
            }
            body += chunk;
        });
        req.on('end', async () => {
            try {
                const data = JSON.parse(body);
                const mensaje = data.mensaje || '';
                const audio_base64 = data.audio_base64 || null;
                if ((mensaje || audio_base64) && client.info) {
                    if (mensaje) {
                        await client.sendMessage(NUMERO_AUTORIZADO, mensaje);
                    }
                    if (audio_base64) {
                        try {
                            const audioMedia = new MessageMedia('audio/mpeg', audio_base64, 'jarvis.mp3');
                            await client.sendMessage(NUMERO_AUTORIZADO, audioMedia,
                                { sendAudioAsVoice: true });
                        } catch (audioErr) {
                            console.error('Error enviando audio WhatsApp:', audioErr.message, audioErr.stack);
                            res.writeHead(500, {'Content-Type': 'application/json'});
                            res.end(JSON.stringify({error: audioErr.message}));
                            return;
                        }
                    }
                    res.writeHead(200, {'Content-Type': 'application/json'});
                    res.end(JSON.stringify({status: 'ok'}));
                } else {
                    res.writeHead(400, {'Content-Type': 'application/json'});
                    res.end(JSON.stringify({error: 'mensaje vacio o cliente no listo'}));
                }
            } catch(e) {
                res.writeHead(500, {'Content-Type': 'application/json'});
                res.end(JSON.stringify({error: e.message}));
            }
        });
    } else {
        res.writeHead(404);
        res.end();
    }
});
alertServer.listen(8001, '127.0.0.1', () => {
    console.log('Servidor alertas JARVIS escuchando en puerto 8001');
});
