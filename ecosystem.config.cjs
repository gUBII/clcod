module.exports = {
  apps: [
    {
      name: 'clcod-4173',
      cwd: '/Users/moofasa/clcod',
      script: 'supervisor.py',
      interpreter: 'python3',
      args: '--config config.json',
      env: {
        PYTHONUNBUFFERED: '1',
        CONFIG: '/Users/moofasa/clcod/config.json',
      },
      autorestart: true,
      watch: false,
      max_memory_restart: '512M',
      out_file: '/Users/moofasa/clcod/.clcod-runtime/pm2-out.log',
      error_file: '/Users/moofasa/clcod/.clcod-runtime/pm2-err.log',
      merge_logs: true,
    },
  ],
};
