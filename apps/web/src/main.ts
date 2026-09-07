import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "@/App.vue";
import { i18n } from "@/app/i18n";
import { router } from "@/app/router";
import "@/shared/tokens.css";

const app = createApp(App);

app.use(createPinia());
app.use(i18n);
app.use(ElementPlus);
app.use(router);

app.mount("#app");
