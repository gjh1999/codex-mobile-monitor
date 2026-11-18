import {fileURLToPath} from 'node:url';
import {defineConfig, searchForWorkspaceRoot} from 'vite';

const cover = fileURLToPath(new URL('../docs/assets/os-x-mavericks-wave.jpg', import.meta.url));

export default defineConfig({
  server: {
    // Use the same cover as the plan without serving the rest of the documentation.
    fs: {allow: [searchForWorkspaceRoot(process.cwd()), cover]},
    proxy: {'/api': 'http://127.0.0.1:8765'},
  },
});
