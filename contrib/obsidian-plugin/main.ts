import {
  App,
  Notice,
  Plugin,
  PluginSettingTab,
  Setting,
  TFile,
  requestUrl,
  RequestUrlParam,
} from "obsidian";

interface RemarkBridgeSettings {
  serverUrl: string;
  apiToken: string;
  retryAttempts: number;
  retryDelayMs: number;
}

const DEFAULT_SETTINGS: RemarkBridgeSettings = {
  serverUrl: "http://localhost:8000",
  apiToken: "",
  retryAttempts: 3,
  retryDelayMs: 2000,
};

export default class RemarkBridgePlugin extends Plugin {
  settings: RemarkBridgeSettings = DEFAULT_SETTINGS;
  private statusBar: HTMLElement | null = null;
  private statusTimer: number | null = null;

  async onload() {
    await this.loadSettings();

    this.addRibbonIcon("tablet", "Push current note to reMarkable", async () => {
      await this.pushActiveNote();
    });

    this.addCommand({
      id: "push-current-note",
      name: "Push current note to reMarkable",
      callback: () => this.pushActiveNote(),
    });

    this.addCommand({
      id: "refresh-sync-status",
      name: "Refresh reMark Bridge status",
      callback: () => this.refreshStatus(),
    });

    this.statusBar = this.addStatusBarItem();
    this.statusBar.setText("reMark: …");
    this.statusBar.addClass("mod-clickable");
    this.statusBar.addEventListener("click", () => this.refreshStatus());

    this.addSettingTab(new RemarkBridgeSettingTab(this.app, this));

    // Poll every 60s once the plugin loads. The first tick fires
    // immediately so the status bar doesn't show a stale "..."
    // until the next interval.
    this.refreshStatus();
    this.statusTimer = window.setInterval(() => this.refreshStatus(), 60_000);
    this.registerInterval(this.statusTimer);
  }

  async onunload() {
    if (this.statusTimer !== null) {
      window.clearInterval(this.statusTimer);
      this.statusTimer = null;
    }
  }

  async loadSettings() {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  async saveSettings() {
    await this.saveData(this.settings);
  }

  private authHeader(): Record<string, string> {
    return {
      Authorization: `Bearer ${this.settings.apiToken}`,
      "Content-Type": "application/json",
    };
  }

  private async bridgeRequest(params: RequestUrlParam): Promise<any> {
    // Retry with a short exponential back-off. `requestUrl` already
    // times out internally, so the only failures we catch here are
    // genuine network / server errors.
    let lastError: unknown = null;
    const attempts = Math.max(1, this.settings.retryAttempts);

    for (let attempt = 0; attempt < attempts; attempt++) {
      try {
        const resp = await requestUrl(params);
        if (resp.status >= 400) {
          throw new Error(`HTTP ${resp.status}: ${resp.text?.slice(0, 200) ?? ""}`);
        }
        return resp.json ?? JSON.parse(resp.text || "{}");
      } catch (err) {
        lastError = err;
        if (attempt < attempts - 1) {
          const wait = this.settings.retryDelayMs * Math.pow(2, attempt);
          await new Promise((resolve) => setTimeout(resolve, wait));
        }
      }
    }
    throw lastError ?? new Error("Bridge request failed");
  }

  async pushActiveNote() {
    const active = this.app.workspace.getActiveFile();
    if (!active || !(active instanceof TFile)) {
      new Notice("reMark: no active note to push");
      return;
    }

    if (!this.settings.apiToken) {
      new Notice("reMark: set a bridge token in plugin settings first");
      return;
    }

    try {
      const data = await this.bridgeRequest({
        url: `${this.settings.serverUrl.replace(/\/$/, "")}/api/push`,
        method: "POST",
        headers: this.authHeader(),
        body: JSON.stringify({ vault_path: active.path }),
        throw: false,
      });
      if (data?.queued) {
        new Notice(`reMark: queued "${active.basename}" for push`);
      } else {
        new Notice(`reMark: push rejected — ${JSON.stringify(data)}`);
      }
    } catch (err: any) {
      new Notice(`reMark: push failed — ${err?.message ?? err}`);
    }
  }

  async refreshStatus() {
    if (!this.statusBar) return;

    if (!this.settings.apiToken) {
      this.statusBar.setText("reMark: no token");
      return;
    }

    try {
      const data = await this.bridgeRequest({
        url: `${this.settings.serverUrl.replace(/\/$/, "")}/api/status`,
        method: "GET",
        headers: this.authHeader(),
        throw: false,
      });
      const sync = data?.sync ?? {};
      const queue = data?.queue ?? {};
      const failed = queue.failed ?? 0;
      const pending = queue.pending ?? 0;

      let label = `reMark: ${sync.synced ?? 0} synced`;
      if (pending) label += ` · ${pending} pending`;
      if (failed) label += ` · ⚠ ${failed} failed`;
      this.statusBar.setText(label);
    } catch (err: any) {
      this.statusBar.setText("reMark: offline");
    }
  }
}

class RemarkBridgeSettingTab extends PluginSettingTab {
  plugin: RemarkBridgePlugin;

  constructor(app: App, plugin: RemarkBridgePlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl("h2", { text: "reMark Bridge" });

    new Setting(containerEl)
      .setName("Server URL")
      .setDesc("Where the reMark Bridge web service is running. Include the scheme and port.")
      .addText((text) =>
        text
          .setPlaceholder("http://localhost:8000")
          .setValue(this.plugin.settings.serverUrl)
          .onChange(async (value) => {
            this.plugin.settings.serverUrl = value.trim();
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("API token")
      .setDesc(
        "Issue a token on the server with `remark-bridge bridge-token issue --label obsidian`.",
      )
      .addText((text) =>
        text
          .setPlaceholder("paste the bearer token here")
          .setValue(this.plugin.settings.apiToken)
          .onChange(async (value) => {
            this.plugin.settings.apiToken = value.trim();
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Retry attempts")
      .setDesc("Number of times a failing request is retried before giving up.")
      .addSlider((slider) =>
        slider
          .setLimits(1, 6, 1)
          .setValue(this.plugin.settings.retryAttempts)
          .setDynamicTooltip()
          .onChange(async (value) => {
            this.plugin.settings.retryAttempts = value;
            await this.plugin.saveSettings();
          }),
      );

    new Setting(containerEl)
      .setName("Retry initial delay (ms)")
      .setDesc("First retry waits this long; subsequent retries double the delay.")
      .addText((text) =>
        text
          .setValue(String(this.plugin.settings.retryDelayMs))
          .onChange(async (value) => {
            const n = Number.parseInt(value, 10);
            if (Number.isFinite(n) && n > 0) {
              this.plugin.settings.retryDelayMs = n;
              await this.plugin.saveSettings();
            }
          }),
      );
  }
}
