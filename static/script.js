$(document).ready(function() {
    console.log('jQuery loaded and ready');
    $('#chat-form').submit(function(e) {
        console.log('Form submit triggered');
        e.preventDefault();
        var message = $('#message').val().trim();
        if (!message) return;

        console.log('Sending:', message);
        $.ajax({
            url: '/send',
            type: 'POST',
            data: { message: message },
            dataType: 'json',
            success: function(data) {
                console.log('Response:', data);
                updateChat(data.messages, data.state);
                $('#message').val('');
            },
            error: function(xhr) {
                console.error('Error:', xhr.responseText);
            }
        });
    });

    function updateChat(messages, state) {
        $('#messages').empty();
        messages.forEach(function(msg) {
            $('#messages').append(
                `<div class="chat-message ${msg.role}">${msg.content}</div>`
            );
        });
        if (state.typing) {
            $('#messages').append('<div class="typing-indicator">Monika is typing...</div>');
        }
        $('#messages').append(
            `<div class="mood-bar"><div class="mood-progress" style="width: ${state.progress * 100}%;"></div></div>`
        );
        $('#messages').scrollTop($('#messages')[0].scrollHeight);
    }

    $('#messages').scrollTop($('#messages')[0].scrollHeight);
});

function toggleMode(mode) {
    console.log('Toggling mode to:', mode);
    $.ajax({
        url: '/toggle_mode',
        type: 'POST',
        data: { mode: mode },
        dataType: 'json',
        success: function(data) {
            console.log('Toggle Response:', data);
            updateChat(data.messages, data.state);
        },
        error: function(xhr) {
            console.error('Error:', xhr.responseText);
        }
    });
}