import { createApp } from "vue";
import App from "./App.vue";
import "./style.css";
import { createVuetify } from "vuetify";
import * as components from "vuetify/components";
import * as directives from "vuetify/directives";
import "vuetify/styles";
import { zhHans } from "vuetify/locale";
import { ChevronDown, X, CircleCheck, CircleAlert, Info, TriangleAlert, Square, SquareCheck, Minus, Circle, CircleDot, ChevronLeft, ChevronRight } from "@lucide/vue";

createApp(App).use(createVuetify({
  components, directives,
  locale: { locale: "zhHans", messages: { zhHans } },
  icons: { aliases: {
    dropdown: ChevronDown, clear: X, close: X, complete: CircleCheck,
    success: CircleCheck, error: CircleAlert, info: Info, warning: TriangleAlert,
    checkboxOn: SquareCheck, checkboxOff: Square, checkboxIndeterminate: Minus,
    radioOn: CircleDot, radioOff: Circle, prev: ChevronLeft, next: ChevronRight,
  } },
  theme: { defaultTheme: "light", themes: { light: { colors: {
    primary: "#087f75", secondary: "#4d5968", error: "#b44337", background: "#fff",
  } } } },
  defaults: { VBtn: { style: "letter-spacing: 0", rounded: "sm" },
    VTextField: { variant: "outlined", density: "compact", hideDetails: "auto" },
    VSelect: { variant: "outlined", density: "compact", hideDetails: "auto" } },
})).mount("#app");
