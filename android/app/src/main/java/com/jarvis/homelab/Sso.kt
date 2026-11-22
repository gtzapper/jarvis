package com.jarvis.homelab

import android.annotation.SuppressLint
import android.net.Uri
import android.webkit.CookieManager
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView

/**
 * Single sign-on (Authentik and other OpenID Connect providers) inside the app: the provider's own sign-in page
 * opens in a web view; when it sends you back to Jarvis, the app keeps Jarvis's session cookie and closes it.
 * The app never sees your provider password.
 */
@SuppressLint("SetJavaScriptEnabled")
@Composable
fun SsoScreen(server: String, onDone: (String?) -> Unit, onCancel: () -> Unit) {
    val base = Uri.parse(server)
    BackHandler { onCancel() }
    Column(Modifier.fillMaxSize().statusBarsPadding()) {
        Row(Modifier.fillMaxWidth().padding(horizontal = 8.dp), verticalAlignment = Alignment.CenterVertically) {
            Text("Sign in to Jarvis", style = MaterialTheme.typography.titleMedium, modifier = Modifier.weight(1f).padding(start = 8.dp))
            TextButton(onCancel) { Text("Cancel") }
        }
        AndroidView(modifier = Modifier.fillMaxSize(), factory = { ctx ->
            CookieManager.getInstance().apply { setAcceptCookie(true); removeAllCookies(null) }
            WebView(ctx).apply {
                settings.javaScriptEnabled = true   // sign-in pages need it
                settings.domStorageEnabled = true
                webViewClient = object : WebViewClient() {
                    var finished = false
                    override fun onPageFinished(view: WebView, url: String) {
                        val u = Uri.parse(url)
                        val home = u.host == base.host && u.port == base.port && (u.path.isNullOrEmpty() || u.path == "/")
                        if (home && !finished) {  // back on the Jarvis dashboard: signed in
                            finished = true
                            onDone(CookieManager.getInstance().getCookie(server))
                            view.stopLoading()
                        }
                    }
                }
                loadUrl(server.trimEnd('/') + "/login")  // Jarvis's own sign-in page: every provider, or a password
            }
        })
    }
}
