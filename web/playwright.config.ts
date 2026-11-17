import {defineConfig} from '@playwright/test';
export default defineConfig({testDir:'./tests',workers:1,use:{baseURL:'http://127.0.0.1:5173',channel:'msedge',headless:true},webServer:{command:'node node_modules/vite/bin/vite.js --host 127.0.0.1',url:'http://127.0.0.1:5173',reuseExistingServer:false,gracefulShutdown:{signal:'SIGTERM',timeout:3000}}});
