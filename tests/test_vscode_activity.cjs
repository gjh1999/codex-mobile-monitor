const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const Module = require('node:module');
const path = require('node:path');
const test = require('node:test');

test('one reporter per local root, explicit close on folder removal and shutdown', async () => {
    const processes = [];
    const local = fsPath => ({ uri: { scheme: 'file', fsPath } });
    let reconcile;
    const vscode = {
        window: { createOutputChannel: () => ({ append() {}, appendLine() {} }) },
        workspace: {
            workspaceFolders: [local('C:\\A'), local('D:\\B'), { uri: { scheme: 'vscode-remote' } }],
            onDidChangeWorkspaceFolders(callback) {
                reconcile = callback;
                return { dispose() { reconcile = undefined; } };
            }
        }
    };
    const load = Module._load;
    Module._load = (name, ...rest) => {
        if (name === 'vscode') return vscode;
        if (name !== 'node:child_process') return load(name, ...rest);
        return { spawn(program, args) {
            const child = new EventEmitter();
            child.stderr = { setEncoding: () => new EventEmitter() };
            child.stdin = new EventEmitter();
            child.stdin.end = data => {
                child.closeCommand = data;
                child.emit('close', 0);
            };
            processes.push({ program, root: args.at(-1), child });
            return child;
        } };
    };
    const initial = process.env.LANXI_INSTALL_DIR;
    process.env.LANXI_INSTALL_DIR = path.resolve('.');
    try {
        const extension = require('../ide/vscode/extension.js');
        extension.activate({ subscriptions: [] });
        assert.deepEqual(processes.map(p => p.root), ['C:\\A', 'D:\\B']);
        vscode.workspace.workspaceFolders = [local('D:\\B'), local('D:\\C')];
        reconcile();
        assert.equal(processes[0].child.closeCommand, 'close\n');
        assert.equal(processes[1].child.closeCommand, undefined);
        assert.deepEqual(processes.map(p => p.root), ['C:\\A', 'D:\\B', 'D:\\C']);
        await extension.deactivate();
        assert.equal(processes[1].child.closeCommand, 'close\n');
        assert.equal(processes[2].child.closeCommand, 'close\n');
    } finally {
        Module._load = load;
        if (initial === undefined) delete process.env.LANXI_INSTALL_DIR;
        else process.env.LANXI_INSTALL_DIR = initial;
    }
});
