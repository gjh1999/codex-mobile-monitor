const { spawn } = require('node:child_process');
const path = require('node:path');
const { existsSync } = require('node:fs');
const vscode = require('vscode');

const reporters = new Map();
let foldersChanged;
let output;

function activate(context) {
    const install = process.env.LANXI_INSTALL_DIR;
    if (!install || !path.isAbsolute(install)) return;
    output = vscode.window.createOutputChannel('澜汐项目活动');
    context.subscriptions.push(output);

    function reconcile() {
        const roots = new Set((vscode.workspace.workspaceFolders || [])
            .map(folder => folder.uri)
            .filter(uri => uri.scheme === 'file')
            .map(uri => uri.fsPath));
        for (const [root, reporter] of reporters) {
            if (!roots.has(root)) {
                reporter.stop();
                reporters.delete(root);
            }
        }
        for (const root of roots) {
            if (!reporters.has(root)) reporters.set(root, start(install, root));
        }
    }

    foldersChanged = vscode.workspace.onDidChangeWorkspaceFolders(reconcile);
    context.subscriptions.push(foldersChanged);
    reconcile();
}

function start(install, root) {
    const portable = path.join(install, 'Lanxi-IdeActivity.exe');
    const havePortable = existsSync(portable);
    const executable = havePortable ? portable : path.join(install, '.venv', 'Scripts', 'python.exe');
    const args = ['--source', 'vscode', '--root', root];
    if (!havePortable) args.unshift(path.join(install, 'ide_activity.py'));
    const child = spawn(executable, args, {
        cwd: install, windowsHide: true, stdio: ['pipe', 'ignore', 'pipe'],
        env: { ...process.env, PYTHONIOENCODING: 'utf-8' }
    });
    child.stderr.setEncoding('utf8').on('data', text => {
        output.append(text);
        vscode.window.showWarningMessage(text.trim());
    });
    child.stdin.on('error', () => {});
    const exited = new Promise(resolve => {
        child.once('close', resolve);
        child.once('error', error => {
            output.appendLine(error.message);
            resolve();
        });
    });
    return {
        stop() {
            if (!child.stdin.destroyed) child.stdin.end('close\n');
            return exited;
        }
    };
}

async function deactivate() {
    foldersChanged?.dispose();
    const exited = Array.from(reporters.values(), reporter => reporter.stop());
    reporters.clear();
    let timer;
    try {
        await Promise.race([Promise.all(exited), new Promise(resolve => { timer = setTimeout(resolve, 4500); })]);
    } finally {
        clearTimeout(timer);
    }
}

module.exports = { activate, deactivate };
