package lanxi.activity;

import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.project.ProjectManagerListener;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public final class LanxiProjectListener implements ProjectManagerListener {
    private static final Logger LOG = Logger.getInstance(LanxiProjectListener.class);
    private final Map<Project, Process> reporters = new ConcurrentHashMap<>();

    @Override
    public void projectOpened(Project project) {
        String install = System.getenv("LANXI_INSTALL_DIR");
        String root = project.getBasePath();
        if (install == null || install.isBlank() || root == null) return;
        Path directory = Path.of(install);
        if (!directory.isAbsolute()) return;
        reporters.computeIfAbsent(project, ignored -> {
            try {
                return new ProcessBuilder(
                    directory.resolve(".venv/Scripts/python.exe").toString(),
                    directory.resolve("ide_activity.py").toString(), "--source", "pycharm", "--root", root
                ).directory(directory.toFile())
                 .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                 .redirectError(ProcessBuilder.Redirect.INHERIT)
                 .start();
            } catch (IOException error) {
                LOG.warn("Could not start Lanxi activity reporter", error);
                return null;
            }
        });
    }

    @Override
    public void projectClosed(Project project) {
        Process reporter = reporters.remove(project);
        if (reporter == null || !reporter.isAlive()) return;
        try (var input = reporter.getOutputStream()) {
            input.write("close\n".getBytes(StandardCharsets.US_ASCII));
        } catch (IOException error) {
            LOG.warn("Could not send the Lanxi project close event", error);
        }
    }
}
